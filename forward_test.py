#!/usr/bin/env python3
"""
Forward test — turn APPROVED trades into a real, paper equity curve.

The loop is: you approve a trade -> the orchestrator writes an order ticket and logs the
disposition (signal_log.jsonl). This module OPENS a paper position from each approved trade,
MARKS it against the live datafeed each day, and CLOSES it at its expiry / horizon / stop —
writing the realized return to benchmarks/outcomes.jsonl. The existing Performance Analyst
(performance.py) then joins those outcomes to decision_log.jsonl and draws the equity curve.
Nothing here trades or moves money; it only marks what you already approved.

Model (v1, honest about its resolution): a trade is graded on its UNDERLYING's directional
move, net of a cost assumption — the same first-order model the reporter already uses. It
answers "did the directional call work?", not "what did the exact multi-leg structure mark at."
Option-structure-precise marking (Black-Scholes per leg) is a later refinement; this is the
version that starts drawing a real curve from day one.

Durable state (on the Railway volume): positions/forward_book.json (the open+closed book).
Reads:  benchmarks/decision_log(.seed).jsonl, benchmarks/signal_log.jsonl, orders/*.json
Writes: benchmarks/outcomes.jsonl (append, once per close), then regenerates the report.

Pure standard library + the datafeed.  Run:  python3 forward_test.py
"""
from __future__ import annotations
import json, os
from datetime import datetime, timezone, date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
BENCH = BASE / "benchmarks"
ORDERS = BASE / "orders"
POSITIONS = BASE / "positions"
BOOK = POSITIONS / "forward_book.json"

DEFAULT_HORIZON_DAYS = int(os.environ.get("FWD_HORIZON_DAYS", "30"))   # equity-vote hold if no expiry
COST_BPS = int(os.environ.get("FWD_COST_BPS", "10"))                    # round-trip cost assumption


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> date:
    return datetime.now(timezone.utc).date()


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for ln in path.read_text().splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


def load_book() -> dict:
    if BOOK.exists():
        try:
            return json.loads(BOOK.read_text())
        except Exception:
            pass
    return {"positions": {}, "outcomes_written": [], "last_run": None}


def save_book(book: dict) -> None:
    POSITIONS.mkdir(parents=True, exist_ok=True)
    book["last_run"] = now_iso()
    tmp = BOOK.with_suffix(".tmp")
    tmp.write_text(json.dumps(book, indent=2))
    tmp.replace(BOOK)


def decisions() -> dict:
    d = {r["decision_id"]: r for r in read_jsonl(BENCH / "decision_log.seed.jsonl")}
    d.update({r["decision_id"]: r for r in read_jsonl(BENCH / "decision_log.jsonl")})
    return d


def approved_opens() -> dict:
    """Directional opens the human APPROVED — from the shadow log, joined to the decision.
    Excludes 'manage' (exits) and 'basis' (market-neutral, different P&L model)."""
    dec = decisions()
    out = {}
    for s in read_jsonl(BENCH / "signal_log.jsonl"):
        if s.get("human") != "approved" or s.get("kind") in ("manage", "basis"):
            continue
        did = s.get("decision_id")
        if not did:
            continue
        d = dec.get(did, {})
        out[did] = {
            "decision_id": did,
            "ticker": (s.get("ticker") or d.get("ticker") or did.split("-")[0]).upper(),
            "side": s.get("side") or d.get("side"),
            "size_pct_nav": s.get("size_pct_nav") or d.get("size_pct_nav"),
            "approval_id": s.get("approval_id"),
        }
    return out


def _ticket(approval_id: str | None) -> dict:
    if not approval_id:
        return {}
    f = ORDERS / f"{approval_id}.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            return {}
    return {}


def _horizon_and_stop(approval_id: str | None):
    """Close-by date and stop from the order ticket: the latest leg expiry if present,
    else a default horizon. Returns (close_by: date, stop: float|None)."""
    t = _ticket(approval_id)
    stop = t.get("stop_loss")
    stop = float(stop) if isinstance(stop, (int, float)) else None
    exps = []
    for leg in (t.get("structure") or {}).get("legs", []) or []:
        e = leg.get("expiry")
        try:
            exps.append(date.fromisoformat(str(e)))
        except Exception:
            continue
    close_by = max(exps) if exps else (today() + timedelta(days=DEFAULT_HORIZON_DAYS))
    return close_by, stop


def _mark(feed, ticker: str):
    try:
        q = feed.get_quotes([ticker]).get(ticker.upper(), {})
        px = q.get("last") or q.get("mid") or q.get("bid") or q.get("ask")
        return float(px) if px is not None else None
    except Exception:
        return None


def _append_outcome(rec: dict) -> None:
    BENCH.mkdir(parents=True, exist_ok=True)
    with open(BENCH / "outcomes.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


def main() -> int:
    book = load_book()
    positions = book["positions"]
    written = set(book.get("outcomes_written", []))
    try:
        from datafeed import DataFeed
        feed = DataFeed()
    except Exception as e:
        print(f"[forward] datafeed unavailable ({e}); cannot mark — will retry next run")
        return 1

    opened = marked = closed = 0
    tdy = today()

    # 1) OPEN new approved trades (entry marked at the first post-approval close)
    for did, t in approved_opens().items():
        if did in positions:
            continue
        px = _mark(feed, t["ticker"])
        if px is None:
            continue                                  # no quote yet; try next run
        close_by, stop = _horizon_and_stop(t["approval_id"])
        positions[did] = {
            "decision_id": did, "ticker": t["ticker"], "side": t["side"],
            "size_pct_nav": t["size_pct_nav"], "entry_date": tdy.isoformat(),
            "entry_px": px, "stop": stop, "close_by": close_by.isoformat(),
            "status": "open", "last_px": px, "last_marked": tdy.isoformat(),
        }
        opened += 1

    # 2) MARK open positions; CLOSE on expiry / horizon / stop
    for did, p in positions.items():
        if p.get("status") != "open":
            continue
        px = _mark(feed, p["ticker"])
        if px is None:
            continue
        p["last_px"] = px
        p["last_marked"] = tdy.isoformat()
        marked += 1
        side = (p.get("side") or "").lower()
        stop = p.get("stop")
        close_by = date.fromisoformat(p["close_by"])
        stop_hit = (stop is not None and (
            (side == "long" and px <= stop) or (side == "short" and px >= stop)))
        expired = tdy >= close_by
        if stop_hit or expired:
            entry = float(p["entry_px"]) or px
            long_ret = round((px / entry - 1.0) * 100.0, 4)     # underlying long-return; reporter signs it
            p["status"] = "closed"
            p["exit_px"] = px
            p["exit_date"] = tdy.isoformat()
            p["exit_reason"] = "stop" if stop_hit else "expiry/horizon"
            p["realized_long_return_pct"] = long_ret
            if did not in written:                              # idempotent: one outcome per trade
                _append_outcome({
                    "decision_id": did, "ticker": p["ticker"],
                    "long_return_pct": long_ret, "cost_bps": COST_BPS,
                    "opened": p["entry_date"], "closed": tdy.isoformat(),
                    "entry_px": entry, "exit_px": px, "reason": p["exit_reason"],
                    "marked_at": now_iso(),
                })
                written.add(did)
            closed += 1

    book["outcomes_written"] = sorted(written)
    save_book(book)

    n_open = sum(1 for p in positions.values() if p.get("status") == "open")
    print(f"[forward] opened {opened} · marked {marked} · closed {closed} · "
          f"open now {n_open} · total tracked {len(positions)} (src {feed.active()})")

    # 3) Regenerate the human-facing report + equity curve from the realized outcomes.
    try:
        import performance
        performance.main()
    except SystemExit:
        pass
    except Exception as e:
        print(f"[forward] report regen skipped: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
