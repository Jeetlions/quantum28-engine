#!/usr/bin/env python3
"""
Basis Trader — a market-NEUTRAL relative-value agent for single-stock futures (SSF).

It is not a voter and not a directional strategist. It scans listed SSF contracts against their
spot, computes the fair (cost-of-carry) forward, and locks the mispricing when it exceeds costs:

  * CASH-AND-CARRY (future too rich / contango): BUY the spot, SHORT the future. You earn the
    convergence — the implied repo you receive beats your funding cost. Near risk-free.
  * REVERSE (future too cheap / backwardation): SHORT the spot, BUY the future. You earn when the
    implied repo is below what you can lend the short proceeds at — net of borrow cost.

Fair forward (simple carry):  F* = S * (1 + (r - q) * T),   T = days/365
  r = funding/repo rate (annual), q = dividend yield (annual).
Implied repo (what the market pays you to carry):  ((F/S - 1) / T) + q
Edge (cash-and-carry) = implied_repo - r - costs;  reverse edge = r - implied_repo - borrow - costs.

Why it matters (the crash trade): when a name gaps down, the front basis often DISLOCATES while
long-dated futures correct far less — so the term structure gaps open and the carry gets rich. This
agent scans every listed expiry and flags the richest/cheapest, per your thesis.

NOT risk-free — the caveats are real: dividend surprises, short-borrow recall / hard-to-borrow fees,
funding-rate moves, early convergence, and margin. So the edge is charged for costs and only the
clean dislocations pass. Every trade still routes through the HITL — the agent never trades.

Pure standard library.  Run:  python3 basis.py
"""
from __future__ import annotations
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path
import orchestrator as O   # reuse HITL: PENDING, notify_telegram, atomic_write_json, now_iso

BASE = Path(__file__).resolve().parent
UNIV = BASE / "basis" / "ssf_universe.json"
OUT = BASE / "basis"

MIN_EDGE_ANN = 0.015     # 1.5% annualized net edge required to act (below this, costs eat it)
COST_BPS = 8             # round-trip execution + slippage on the pair, bps of notional
# CME single-stock futures are not tradable until they list (Sept 2026). Until the go-live date the
# agent MONITORS and scores the basis but produces NO order tickets. Set via env or edit this date.
SSF_GO_LIVE = os.environ.get("SSF_GO_LIVE", "2026-09-15")


def ssf_live() -> bool:
    try:
        return datetime.now(timezone.utc).date().isoformat() >= SSF_GO_LIVE
    except Exception:
        return False


def annualize(x_period, T):
    return x_period / T if T > 1e-9 else 0.0


def evaluate(name: dict, cost_bps: float = COST_BPS):
    """Return a list of per-expiry basis assessments for one underlying."""
    S = float(name["spot"]); q = float(name.get("dividend_yield", 0.0))
    r = float(name.get("funding_rate", 0.05)); borrow = float(name.get("borrow_fee", 0.005))
    cost = cost_bps / 10000.0
    rows = []
    for c in name.get("futures", []):
        T = float(c["expiry_days"]) / 365.0
        F = float(c["price"])
        F_fair = S * (1 + (r - q) * T)
        implied_repo = annualize(F / S - 1.0, T) + q          # carry the market pays you
        basis = F - S
        cc_edge = implied_repo - r - annualize(cost, T)       # buy spot / short future
        rev_edge = r - implied_repo - borrow - annualize(cost, T)   # short spot / buy future
        if cc_edge >= MIN_EDGE_ANN and cc_edge >= rev_edge:
            action, edge = "cash_and_carry", cc_edge
            legs = [{"leg": "buy", "instrument": "spot", "ticker": name["ticker"]},
                    {"leg": "sell", "instrument": "future", "ticker": name["ticker"], "expiry_days": c["expiry_days"]}]
            why = (f"future rich (contango): F {F} vs fair {F_fair:.2f}; implied repo {implied_repo*100:.1f}% "
                   f"> funding {r*100:.1f}% -> buy spot / short the {c['expiry_days']}d future, lock {edge*100:.1f}%/yr")
        elif rev_edge >= MIN_EDGE_ANN:
            action, edge = "reverse", rev_edge
            legs = [{"leg": "sell", "instrument": "spot", "ticker": name["ticker"]},
                    {"leg": "buy", "instrument": "future", "ticker": name["ticker"], "expiry_days": c["expiry_days"]}]
            why = (f"future cheap (backwardation): F {F} vs fair {F_fair:.2f}; implied repo {implied_repo*100:.1f}% "
                   f"< funding {r*100:.1f}% -> short spot / buy the {c['expiry_days']}d future, net {edge*100:.1f}%/yr "
                   f"after {borrow*100:.1f}% borrow")
        else:
            action, edge, legs, why = "no_edge", max(cc_edge, rev_edge), [], \
                f"basis within costs (best net edge {max(cc_edge, rev_edge)*100:.1f}%/yr < {MIN_EDGE_ANN*100:.1f}%)"
        rows.append({"ticker": name["ticker"], "expiry_days": c["expiry_days"], "spot": S, "future": F,
                     "fair_forward": round(F_fair, 2), "basis": round(basis, 2),
                     "implied_repo_pct": round(implied_repo * 100, 2), "funding_pct": round(r * 100, 2),
                     "action": action, "net_edge_ann_pct": round(edge * 100, 2), "legs": legs, "rationale": why})
    return rows


def scan(universe, cost_bps: float = COST_BPS):
    allrows = []
    for name in universe:
        allrows.extend(evaluate(name, cost_bps))
    trades = [r for r in allrows if r["action"] != "no_edge"]
    trades.sort(key=lambda r: r["net_edge_ann_pct"], reverse=True)
    return allrows, trades


def suspend_basis(t: dict, caller: str = "basis-trader") -> str:
    aid = f"BASIS-{t['ticker']}-{t['expiry_days']}d-{O.now_iso().replace(':','').replace('-','')[:14]}"
    record = {"approval_id": aid, "status": "awaiting_human", "kind": "basis",
              "requested_by": caller, "created_at": O.now_iso(), "basis_trade": t}
    O.atomic_write_json(O.PENDING / f"{aid}.json", record)
    msg = (f"*Basis {t['ticker']}* — {t['action']}  ({aid})\n"
           f"{t['expiry_days']}d future {t['future']} vs spot {t['spot']} (fair {t['fair_forward']})\n"
           f"net edge {t['net_edge_ann_pct']}%/yr · implied repo {t['implied_repo_pct']}% vs funding {t['funding_pct']}%\n"
           f"{t['rationale']}\nApprove to route the pair to the execution desk.")
    O.notify_telegram(msg, aid)
    return aid


def main():
    if not UNIV.exists():
        print(f"No universe at {UNIV}"); return 1
    universe = json.loads(UNIV.read_text())
    allrows, trades = scan(universe)
    OUT.mkdir(parents=True, exist_ok=True)
    O.atomic_write_json(OUT / "basis_signals.json", {"as_of": O.now_iso(), "assessed": allrows, "trades": trades})
    print(f"[basis] scanned {len(allrows)} contracts across {len(universe)} names; {len(trades)} dislocation(s)\n")
    for r in allrows:
        tag = r["action"].upper() if r["action"] != "no_edge" else "-"
        print(f"  {r['ticker']:5} {r['expiry_days']:>4}d  F {r['future']:>8} vs fair {r['fair_forward']:>8}  "
              f"repo {r['implied_repo_pct']:>5}%  edge {r['net_edge_ann_pct']:>5}%/yr  {tag}")
    if not ssf_live():
        print(f"\n[basis] MONITOR-ONLY — single-stock futures not tradable until {SSF_GO_LIVE}. "
              f"{len(trades)} dislocation(s) scored but NO order tickets emitted.")
        return 0
    for t in trades:
        aid = suspend_basis(t)
        print(f"\n[suspend] {t['ticker']} {t['action']} -> pending_approvals/{aid}.json")
    if not trades:
        print("\nNo dislocation beyond costs — nothing to propose.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
