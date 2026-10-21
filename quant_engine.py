#!/usr/bin/env python3
"""
quant_engine.py — the deterministic core of Quantum28, as ONE callable tool.

The split that makes this safe: the GrokBots (the analyst personas) do the JUDGMENT and write
their views; this engine does the ARITHMETIC — the weighted vote, the MIN_CONVICTION gate, the
risk-manager veto/sizing, the Strategist's exact legs, and the order ticket. There is NO model
call in here, so the numbers are reproducible and auditable every time: the same views in always
give the same ticket out. The Chief of Staff orchestrates by calling one subcommand; it never
computes the vote or the strikes in its own head.

Subcommands
  decide  --ticker SYM --views VIEWS.json [--tenor 0DTE]   vote -> structure -> order ticket
  gex     --chain CHAIN.json                               dealer-gamma block (net GEX/flip/walls/pin)

I/O (shared-computer friendly): each input is a JSON file, or '-' to read JSON from stdin.
A `decide` run writes the ticket to $QE_WORKSPACE/tickets/<id>.json (default /workspace) and
prints a human summary plus the JSON, so the Chief of Staff can both store it and relay it.

Reuses the existing modules unchanged: orchestrator.run_risk_manager, strategist.build_structure,
decay_signals.compute_signal, gex_algo.compute_gex. Pure standard library otherwise.
"""
from __future__ import annotations
import argparse, json, os, sys
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE = Path(os.environ.get("QE_WORKSPACE", "/workspace"))
TICKETS = WORKSPACE / "tickets"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path: str) -> dict:
    """Read a JSON input from a file, or from stdin when path is '-'."""
    text = sys.stdin.read() if path == "-" else Path(path).read_text()
    return json.loads(text)


def _legs_line(structure: dict) -> str:
    legs = (structure or {}).get("legs", []) or []
    exps = [l.get("expiry") for l in legs if l.get("expiry")]
    uniq = sorted(set(exps))
    multi = len(uniq) > 1
    parts = []
    for l in legs:
        s = f"{l.get('action')} {l.get('qty', 1)}x {l.get('right')} {l.get('strike')}"
        if multi and l.get("expiry"):
            s += f"@{l['expiry']}"
        parts.append(s)
    line = "  ·  ".join(parts)
    if uniq:
        line += f"   (exp {' / '.join(uniq)})" if multi else f"   (exp {uniq[0]})"
    return line


# ---------------------------------------------------------------------------
# decide — views in, order ticket out (the systematic pipeline, deterministic)
# ---------------------------------------------------------------------------
def decide(ticker: str, views: dict, tenor: str | None = None, compute_inputs: bool = True) -> dict:
    import orchestrator as O
    import strategist

    ticker = ticker.upper()
    if tenor:                                   # route index same-day gamma trades through the 0DTE path
        views.setdefault("options-analyst", {}).setdefault("options", {})["tenor"] = tenor

    # TA & Quant are REASONING bots: if the bot supplied a view, BOUND its numeric levels to the real
    # price range (a hallucinated stop can't poison sizing/strikes); if no bot view, fall back to the
    # computed one. Liquidity stays computed — it's a measurement, not a judgment.
    if compute_inputs and not (tenor == "0DTE" and ticker in strategist.INDEX_TICKERS):
        try:
            import quant_inputs
            candles = quant_inputs.tasty_candles(ticker)
            for seat, fallback in (("technical-analyst", quant_inputs.technical_view),
                                   ("quant-analyst", quant_inputs.quant_view)):
                if isinstance(views.get(seat), dict):
                    views[seat] = quant_inputs.sanitize_view(views[seat], candles)   # bound the bot's levels
                else:
                    fb = fallback(ticker, candles)
                    if not fb.get("_stub"):
                        views[seat] = fb                                             # computed fallback
            if not isinstance(views.get("liquidity-analyst"), dict):
                lv = quant_inputs.liquidity_view(ticker, candles)
                if not lv.get("_stub"):
                    views["liquidity-analyst"] = lv
        except Exception as e:
            print(f"[quant-engine] inputs step skipped ({e}); proceeding with supplied views")

    # 0DTE INDEX: a same-day gamma trade is decided by the dealer-gamma read, NOT the equity vote,
    # so it bypasses run_risk_manager (mirrors the orchestrator) and goes straight to the Strategist.
    if tenor == "0DTE" and ticker in strategist.INDEX_TICKERS:
        structure = strategist.build_structure(ticker, "neutral", 0.0, views)
        if structure.get("strategy") == "no_trade":
            return {"status": "no_trade", "ticker": ticker, "as_of": now_iso(),
                    "reason": (structure.get("rationale") or ["no fresh gamma edge"])[-1],
                    "kind": "0DTE"}
        order_id = f"{ticker}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        return {"order_id": order_id, "status": "awaiting_human", "type": "trade", "kind": "0DTE",
                "ticker": ticker, "side": structure.get("direction_bias", "neutral"),
                "structure": structure, "as_of": now_iso(),
                "rationale": structure.get("rationale", []),
                "note": "0DTE dealer-gamma trade (bypasses the equity vote). NOT auto-executed — human approves."}

    decision = O.run_risk_manager(views, ticker)
    try:
        import learning                               # record who voted what, for the feedback loop
        learning.log_decision(decision, views)
    except Exception:
        pass
    pos = decision["approved_position"]
    tradeable = decision["decision"] in ("approve", "reduce", "neutral") and pos["side"] != "none"

    structure = None
    if tradeable:
        structure = strategist.build_structure(ticker, pos["side"], decision["vote_net"], views)
        for k in ("strategy", "legs", "underlying_type"):      # same output guardrail as the orchestrator
            if k not in structure:
                raise ValueError(f"strategist returned a malformed ticket (missing '{k}')")
        if structure.get("strategy") == "no_trade":            # 0DTE stale / no edge -> stand aside
            structure = None

    if structure is None:
        return {
            "status": "no_trade",
            "ticker": ticker,
            "as_of": now_iso(),
            "agreement": decision["agreement"],
            "decision": decision["decision"],
            "vote_net": decision["vote_net"],
            "reason": decision["rationale"][-1] if decision.get("rationale") else "no tradeable structure",
            "rationale": decision.get("rationale", []),
        }

    order_id = f"{ticker}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    return {
        "order_id": order_id,
        "status": "awaiting_human",            # a person approves before any broker order — never auto-executed
        "type": "trade",
        "ticker": ticker,
        "side": pos["side"],
        "size_pct_nav": pos["size_pct_nav"],
        "max_loss_pct_nav": pos.get("max_loss_pct_nav", 0.0),
        "stop_loss": decision["stop_loss"],
        "agreement": decision["agreement"],
        "vote_net": decision["vote_net"],
        "structure": structure,
        "put_write": decision.get("put_write"),
        "call_write": decision.get("call_write"),
        "profit_target": decision.get("profit_target"),
        "rationale": decision["rationale"],
        "as_of": now_iso(),
        "note": "NOT auto-executed — routed to a human for approval before any broker order.",
    }


def _print_ticket(t: dict) -> None:
    if t.get("status") == "no_trade":
        extra = ""
        if "vote_net" in t:
            extra = f" (vote {t['vote_net']:+.2f}, {t.get('agreement')}/{t.get('decision')})"
        print(f"[quant-engine] {t['ticker']}: NO TRADE — {t.get('reason', 'no edge')}{extra}")
        return
    s = t.get("structure", {}) or {}
    head = f"[quant-engine] {t['ticker']}: TICKET {t['order_id']}  ({t['status']})"
    if t.get("kind"):
        head += f"  · {t['kind']}"
    print(head)
    if "vote_net" in t:                       # equity vote path
        print(f"  {t['side']} {t.get('size_pct_nav','?')}% NAV · stop {t.get('stop_loss')} · "
              f"vote {t['vote_net']:+.2f} ({t.get('agreement')}) · max loss {t.get('max_loss_pct_nav',0)}% NAV")
    else:                                     # 0DTE gamma path (no equity vote)
        print(f"  {t.get('side','neutral')} · dealer-gamma trade (no equity vote)")
    print(f"  {s.get('strategy')} on {s.get('underlying_type')}:")
    print(f"    {_legs_line(s)}")
    net = s.get("net") or {}
    print(f"    net {net.get('type','?')} {net.get('amount','')} · "
          f"max loss {s.get('max_loss','?')} · max gain {s.get('max_gain','?')}")
    if t.get("rationale"):
        print(f"  why: {t['rationale'][-1]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Quantum28 deterministic quant engine (views -> ticket).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("decide", help="vote + structure + order ticket from analyst views")
    d.add_argument("--ticker", required=True)
    d.add_argument("--views", required=True, help="JSON file of analyst views, or '-' for stdin")
    d.add_argument("--tenor", default=None, help="e.g. 0DTE for index same-day gamma trades")
    d.add_argument("--no-compute", action="store_true",
                   help="skip the Tasty-computed technical/quant/liquidity seats (use supplied views as-is)")

    g = sub.add_parser("gex", help="dealer-gamma block from a normalized chain")
    g.add_argument("--chain", required=True, help="JSON chain {symbol,spot,legs[]}, or '-'")

    a = ap.parse_args()

    if a.cmd == "decide":
        views = _load(a.views)
        ticket = decide(a.ticker, views, tenor=a.tenor, compute_inputs=not a.no_compute)
        if ticket.get("order_id"):
            TICKETS.mkdir(parents=True, exist_ok=True)
            (TICKETS / f"{ticket['order_id']}.json").write_text(json.dumps(ticket, indent=2))
        _print_ticket(ticket)
        print("\n--- JSON ---")
        print(json.dumps(ticket, indent=2))
        return 0

    if a.cmd == "gex":
        import gex_algo
        block = gex_algo.compute_gex(_load(a.chain))
        print(json.dumps(block, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
