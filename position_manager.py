#!/usr/bin/env python3
"""
Position Manager — the lifecycle CLOSER (the Strategist opens; this closes/rolls).

Runs on a schedule over the OPEN book. For each live structure it re-scores against fresh
analyst views, current P&L, and time-to-expiry, and emits CLOSE / ROLL / TRIM / HOLD. Any
non-HOLD action is suspended and sent to Telegram for your APPROVE / DISAPPROVE — the same
HITL as an open. A managed exit also lands in the signal log, so early profit-taking shows
up in the benchmark (it usually helps Sortino and shrinks drawdown).

Exit rules (first match wins, risk first):
  1. RISK      — an overlay veto fired (regime turned), the stop level was tested, or a
                 short strike was breached.
  2. VIEW      — (directional trades only) the voters' direction flipped, or conviction
                 decayed below the re-evaluation floor.
  3. PROFIT    — enough accumulated: credit structures at >=50% of max profit, debit at >=60%.
  4. TIME      — roll the short leg of a calendar/diagonal once the event has passed; close
                 any short-gamma (credit) structure a few days before expiry to avoid pin risk.
  5. HOLD      — thesis intact, not enough profit, time remaining.

Pure standard library.  Run:  python3 position_manager.py
"""
from __future__ import annotations
import glob, json
from pathlib import Path
import orchestrator as O   # reuse notify_telegram, PENDING, ORDERS, BENCH, atomic_write_json, now_iso

BASE = Path(__file__).resolve().parent
POS = BASE / "positions"

REEVAL_MIN = 0.20     # |vote_net| below this = the directional view has decayed
GAMMA_DTE = 7         # close short-gamma (credit) structures within this many days of expiry
PROFIT_CREDIT = 0.50  # take profit on credit structures at 50% of max
PROFIT_DEBIT = 0.60   # take profit on debit structures at 60% of max
DIRECTIONAL = {"vertical_debit", "vertical_credit", "diagonal"}   # have a directional view


def classify(p: dict) -> tuple[str, str]:
    strat = p.get("strategy", "")
    credit = p.get("net_type") == "credit"
    pnl = float(p.get("pnl_pct_of_max", 0))
    dte = int(p.get("dte_left", 999))

    # 1) RISK
    if p.get("veto"):
        return "CLOSE", "overlay veto fired — regime turned against the position"
    stop = p.get("stop")
    side = p.get("entry_side")
    spot = p.get("spot")
    if stop is not None and spot is not None:
        if (side == "long" and spot <= stop) or (side == "short" and spot >= stop):
            return "CLOSE", f"stop/risk level tested (spot {spot} vs stop {stop})"
    if p.get("short_tested"):
        return "CLOSE", "a short strike was tested"

    # 2) VIEW CHANGE (directional structures only — a neutral condor/fly has no direction)
    if side in ("long", "short") and strat in DIRECTIONAL:
        cur = p.get("current_side")
        net = float(p.get("current_net", 0))
        if cur in ("long", "short") and cur != side:
            return "CLOSE", f"view flipped ({side} -> {cur})"
        if abs(net) < REEVAL_MIN:
            return "CLOSE", f"conviction decayed (net {net:+.2f} < {REEVAL_MIN})"

    # 3) PROFIT
    thr = PROFIT_CREDIT if credit else PROFIT_DEBIT
    if pnl >= thr:
        return "CLOSE", f"profit target: captured {int(pnl*100)}% of max (>= {int(thr*100)}% rule)"

    # 4) TIME / STRUCTURE
    if strat in ("calendar", "double_calendar", "diagonal") and p.get("event_passed"):
        return "ROLL", "event passed — roll the short leg to the next cycle"
    if credit and dte <= GAMMA_DTE:
        return "CLOSE", f"{dte} DTE — close short-gamma structure before pin/gamma risk"

    return "HOLD", "thesis intact; insufficient profit; time remaining"


def suspend_manage(p: dict, action: str, reason: str, caller: str) -> str:
    aid = f"MANAGE-{p['ticker']}-{p['position_id']}-{O.datetime.now(O.timezone.utc).strftime('%Y%m%d%H%M%S')}"
    record = {"approval_id": aid, "status": "awaiting_human", "kind": "manage",
              "requested_by": caller, "created_at": O.now_iso(),
              "action": {"position_id": p["position_id"], "ticker": p["ticker"],
                         "strategy": p.get("strategy"), "action": action, "reason": reason,
                         "pnl_pct_of_max": p.get("pnl_pct_of_max"), "dte_left": p.get("dte_left")}}
    O.atomic_write_json(O.PENDING / f"{aid}.json", record)
    msg = (f"*Manage {p['ticker']}* — {action}  ({aid})\n"
           f"{p.get('strategy')} · P&L {int(float(p.get('pnl_pct_of_max',0))*100)}% of max · {p.get('dte_left')} DTE\n"
           f"Reason: {reason}\nApprove to route the {action.lower()} to the execution desk.")
    O.notify_telegram(msg, aid)
    return aid


def main() -> int:
    positions = []
    for f in sorted(glob.glob(str(POS / "open_positions.json"))):
        positions = json.loads(Path(f).read_text())
    if not positions:
        print("No open positions to manage.")
        return 0
    print(f"[position-manager] reviewing {len(positions)} open positions\n")
    actioned = 0
    for p in positions:
        action, reason = classify(p)
        tag = "HOLD " if action == "HOLD" else action.ljust(5)
        print(f"  {p['ticker']:6} {p.get('strategy',''):20} {tag} — {reason}")
        if action != "HOLD":
            suspend_manage(p, action, reason, caller="position-manager")
            actioned += 1
    print(f"\n{actioned} action(s) suspended for approval (Telegram). HOLDs left untouched.")
    print("Approve/disapprove:  python3 orchestrator.py resume --id <MANAGE-id> --reply approve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
