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


def _weight(p: dict) -> float:
    """Fraction of NAV the position risks (size_pct_nav/100); default 2% if unknown."""
    s = p.get("size_pct_nav")
    return float(s) / 100.0 if isinstance(s, (int, float)) else 0.02


def _pos_net_ret(p: dict) -> float:
    """Signed underlying return (%) net of the cost assumption: + if the direction was right.
    Closed positions use the exit mark; open positions use the latest mark (live MTM)."""
    entry = float(p.get("entry_px") or 0)
    if not entry:
        return 0.0
    px = float((p.get("exit_px") if p.get("status") == "closed" else p.get("last_px")) or entry)
    side = (p.get("side") or "").lower()
    dirn = 1 if side == "long" else (-1 if side == "short" else 0)
    return dirn * (px / entry - 1.0) * 100.0 - COST_BPS / 100.0


def _nav_point(positions: list, tdy: date) -> dict:
    """Today's book NAV: realized P&L (locked) + open positions marked live, each weighted by size."""
    real = sum(_weight(p) * _pos_net_ret(p) for p in positions if p.get("status") == "closed")
    unreal = sum(_weight(p) * _pos_net_ret(p) for p in positions if p.get("status") == "open")
    total = real + unreal
    return {"date": tdy.isoformat(), "equity": round(100.0 * (1 + total / 100.0), 4),
            "total_return_pct": round(total, 4), "realized_pct": round(real, 4),
            "unrealized_pct": round(unreal, 4),
            "open": sum(1 for p in positions if p.get("status") == "open"),
            "closed": sum(1 for p in positions if p.get("status") == "closed")}


def write_mtm_report(book: dict) -> None:
    """Overlay a live mark-to-market equity curve onto performance_report.json. Keeps the reporter's
    realized attribution/ratios when present, but the CURVE and the return/drawdown headline reflect
    open positions marked daily — so it moves the day you approve, not only when a trade closes."""
    positions = list(book["positions"].values())
    nav = book.get("nav_series", [])
    if not positions or not nav:
        return
    pr = BENCH / "performance_report.json"
    rep = {}
    if pr.exists():
        try:
            rep = json.loads(pr.read_text())
        except Exception:
            rep = {}
    curve, peak = [], 100.0
    for pt in nav:
        peak = max(peak, pt["equity"])
        dd = round((peak - pt["equity"]) / peak * 100.0, 2) if peak else 0.0
        curve.append({"date": pt["date"], "ticker": "book", "ret_pct": pt.get("total_return_pct", 0.0),
                      "equity": pt["equity"], "cum_return_pct": round(pt["equity"] - 100.0, 2),
                      "drawdown_pct": dd})
    last = curve[-1]
    head = rep.get("headline", {}) or {}
    head.update({
        "total_return_pct": round(last["equity"] - 100.0, 2),
        "max_drawdown_pct": round(max((c["drawdown_pct"] for c in curve), default=0.0), 2),
        "realized_pct": nav[-1].get("realized_pct", 0.0),
        "unrealized_pct": nav[-1].get("unrealized_pct", 0.0),
        "open_positions": nav[-1].get("open", 0),
        "closed_positions": nav[-1].get("closed", 0),
    })
    for k, v in {"sortino_net": 0.0, "sharpe_net": 0.0, "hit_rate_pct": 0.0,
                 "payoff_ratio": None, "cvar_25_pct": 0.0}.items():
        head.setdefault(k, v)
    rep["headline"] = head
    rep["equity_curve"] = curve
    win = rep.get("window", {}) or {}
    win.update({"from": curve[0]["date"], "to": last["date"],
                "graded_trades": nav[-1].get("closed", 0), "open_positions": nav[-1].get("open", 0)})
    rep["window"] = win
    rep["generated_at"] = now_iso()
    rep["mode"] = "forward-test MTM — open positions marked daily; realized P&L locks on close"
    BENCH.mkdir(parents=True, exist_ok=True)
    pr.write_text(json.dumps(rep, indent=2))


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

    # 3) Append today's book NAV (open positions marked live + realized locked), deduped per day.
    if positions:
        pt = _nav_point(list(positions.values()), tdy)
        nav = book.setdefault("nav_series", [])
        if nav and nav[-1]["date"] == pt["date"]:
            nav[-1] = pt
        else:
            nav.append(pt)

    book["outcomes_written"] = sorted(written)
    save_book(book)

    n_open = sum(1 for p in positions.values() if p.get("status") == "open")
    print(f"[forward] opened {opened} · marked {marked} · closed {closed} · "
          f"open now {n_open} · total tracked {len(positions)} (src {feed.active()})")

    # 4) Regenerate the realized report (attribution/ratios), then overlay the live MTM curve.
    try:
        import performance
        performance.main()
    except SystemExit:
        pass
    except Exception as e:
        print(f"[forward] realized report skipped: {e}")
    try:
        write_mtm_report(book)
    except Exception as e:
        print(f"[forward] MTM overlay skipped: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
