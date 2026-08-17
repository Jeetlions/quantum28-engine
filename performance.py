#!/usr/bin/env python3
"""
Performance Analyst — the REPORTER (read-only). It MEASURES; it never steers.

The benchmark (benchmarks/benchmark.py) is the steering wheel: it optimizes an objective
and emits analyst_weights.json for the next vote. This module is the dashboard: it marks the
book through time and publishes a human-facing scorecard — PnL, an equity curve, Sortino AND
Sharpe, max drawdown, hit rate, tail, per-analyst and per-ticker attribution, and a live-book
vs. shadow-book reconciliation. It writes performance_report.json and an equity_curve.svg.

It is strictly read-only over the fund's own logs:
    benchmarks/decision_log(.seed).jsonl   what the framework decided
    benchmarks/outcomes.jsonl              what actually happened (realized returns)
    benchmarks/signal_log.jsonl            the shadow book — skipped signals + managed exits
It does NOT write analyst_weights.json, does NOT change any vote, and does NOT place a trade.
Separating the reporter from the optimizer keeps the fund from marking its own homework.

Pure standard library.  Run:  python3 performance.py
"""
from __future__ import annotations
import json, math, re
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
BENCH = BASE / "benchmarks"
DEFAULT_COST_BPS = 10
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def sign(stance: str) -> int:
    return {"long": 1, "short": -1, "flat": 0, "neutral": 0, "none": 0}.get(stance, 0)


def dparse(decision_id: str) -> str:
    m = DATE_RE.search(decision_id or "")
    return m.group(1) if m else "0000-00-00"


def direction(d: dict) -> int:
    """The position direction the framework actually took: the logged side if present,
    else the sign of the voters' net conviction (matches how the pipeline votes)."""
    side = d.get("side")
    if side in ("long", "short", "neutral", "none", "flat"):
        return sign(side)
    net = sum(sign(c["stance"]) * float(c["conviction"]) for c in d.get("voters", {}).values())
    return 1 if net > 0 else (-1 if net < 0 else 0)


# ---- statistics (per-trade, illustrative — not annualized) -------------------
def _sortino(rets):
    n = len(rets)
    if n == 0:
        return 0.0
    mean = sum(rets) / n
    dd = math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / n)
    return round(mean / dd, 3) if dd > 1e-9 else (9.99 if mean > 0 else 0.0)


def _sharpe(rets):
    n = len(rets)
    if n < 2:
        return 0.0
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    sd = math.sqrt(var)
    return round(mean / sd, 3) if sd > 1e-9 else (9.99 if mean > 0 else 0.0)


def _cvar(rets, q=0.25):
    if not rets:
        return 0.0
    k = max(1, int(math.ceil(q * len(rets))))
    return round(-sum(sorted(rets)[:k]) / k, 2)


def equity_curve(trades):
    """Compound net per-trade returns into an equity index (start 100) with running drawdown."""
    eq, peak, curve = 100.0, 100.0, []
    for t in trades:
        eq *= (1 + t["ret"] / 100.0)
        peak = max(peak, eq)
        dd = (peak - eq) / peak * 100.0
        curve.append({"date": t["date"], "decision_id": t["id"], "ticker": t["ticker"],
                      "ret_pct": round(t["ret"], 2), "equity": round(eq, 2),
                      "cum_return_pct": round(eq - 100.0, 2), "drawdown_pct": round(dd, 2)})
    return curve


def render_svg(curve, path: Path):
    W, H, PAD = 900, 340, 54
    if not curve:
        path.write_text("<svg xmlns='http://www.w3.org/2000/svg' width='900' height='340'></svg>")
        return
    eqs = [c["equity"] for c in curve]
    lo, hi = min(eqs + [100.0]), max(eqs + [100.0])
    rng = (hi - lo) or 1.0
    n = len(curve)
    def X(i): return PAD + (W - 2 * PAD) * (i / max(1, n - 1))
    def Y(v): return PAD + (H - 2 * PAD) * (1 - (v - lo) / rng)
    pts = " ".join(f"{X(i):.1f},{Y(c['equity']):.1f}" for i, c in enumerate(curve))
    base_y = Y(100.0)
    area = f"{PAD},{H-PAD} " + pts + f" {W-PAD},{H-PAD}"
    el = [f"<svg xmlns='http://www.w3.org/2000/svg' width='{W}' height='{H}' viewBox='0 0 {W} {H}'>",
          f"<rect x='0' y='0' width='{W}' height='{H}' fill='#ffffff'/>",
          f"<text x='{PAD}' y='30' font-family='Helvetica,Arial' font-size='18' font-weight='bold' fill='#1F4E79'>Quantum28 — Equity Curve (index = 100 at start)</text>",
          f"<line x1='{PAD}' y1='{base_y:.1f}' x2='{W-PAD}' y2='{base_y:.1f}' stroke='#9aa4b2' stroke-width='1' stroke-dasharray='4 4'/>",
          f"<text x='{W-PAD}' y='{base_y-6:.1f}' font-family='Helvetica,Arial' font-size='11' fill='#9aa4b2' text-anchor='end'>start (100)</text>",
          f"<polygon points='{area}' fill='#2E6DB4' fill-opacity='0.10'/>",
          f"<polyline points='{pts}' fill='none' stroke='#2E6DB4' stroke-width='2.5'/>"]
    for i, c in enumerate(curve):
        el.append(f"<circle cx='{X(i):.1f}' cy='{Y(c['equity']):.1f}' r='3' fill='#2E6DB4'/>")
    end = curve[-1]
    el.append(f"<text x='{X(n-1):.1f}' y='{Y(end['equity'])-10:.1f}' font-family='Helvetica,Arial' font-size='12' font-weight='bold' fill='#2E6DB4' text-anchor='end'>{end['cum_return_pct']:+.1f}%</text>")
    el.append(f"<text x='{PAD}' y='{H-18}' font-family='Helvetica,Arial' font-size='11' fill='#5b6472'>{curve[0]['date']}</text>")
    el.append(f"<text x='{W-PAD}' y='{H-18}' font-family='Helvetica,Arial' font-size='11' fill='#5b6472' text-anchor='end'>{end['date']}</text>")
    el.append("</svg>")
    path.write_text("\n".join(el))


def main() -> int:
    decisions = {d["decision_id"]: d for d in read_jsonl(BENCH / "decision_log.seed.jsonl")}
    decisions.update({d["decision_id"]: d for d in read_jsonl(BENCH / "decision_log.jsonl")})
    outcomes = {o["decision_id"]: o for o in read_jsonl(BENCH / "outcomes.jsonl")}
    if not decisions or not outcomes:
        print("Need decision_log(.seed).jsonl and outcomes.jsonl to report. Nothing to do.")
        return 1

    trades, analyst_pnl, ticker_pnl = [], {}, {}
    for did, d in decisions.items():
        o = outcomes.get(did)
        if not o:
            continue
        dirn = direction(d)
        if dirn == 0:                       # flat / neutral: no directional P&L to mark here
            continue
        R = float(o["long_return_pct"])
        cost = float(o.get("cost_bps", DEFAULT_COST_BPS)) / 100.0
        net = dirn * R - cost
        tkr = d.get("ticker", did.split("-")[0])
        trades.append({"date": dparse(did), "id": did, "ticker": tkr, "dir": dirn, "ret": net})
        ticker_pnl[tkr] = round(ticker_pnl.get(tkr, 0.0) + net, 2)
        for name, call in d.get("voters", {}).items():
            s = sign(call["stance"])
            if s != 0:
                analyst_pnl[name] = round(analyst_pnl.get(name, 0.0) + (s * R - cost), 2)

    trades.sort(key=lambda t: (t["date"], t["id"]))
    curve = equity_curve(trades)
    rets = [t["ret"] for t in trades]
    n = len(rets)
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    total_return = round(curve[-1]["cum_return_pct"], 2) if curve else 0.0
    max_dd = round(max((c["drawdown_pct"] for c in curve), default=0.0), 2)
    hit = round(100 * len(wins) / n, 1) if n else 0.0
    avg_win = round(sum(wins) / len(wins), 2) if wins else 0.0
    avg_loss = round(sum(losses) / len(losses), 2) if losses else 0.0
    payoff = round(avg_win / abs(avg_loss), 2) if losses else None
    cost_drag = round(sum(float(outcomes[t["id"]].get("cost_bps", DEFAULT_COST_BPS)) for t in trades) / (100.0 * n), 3) if n else 0.0

    # live book vs shadow book (skipped signals + managed exits)
    sig = read_jsonl(BENCH / "signal_log.jsonl")
    opens = [s for s in sig if s.get("kind") != "manage"]
    manages = [s for s in sig if s.get("kind") == "manage"]
    shadow = {
        "opens_proposed": len(opens),
        "opens_taken": sum(1 for s in opens if s.get("human") == "approved"),
        "opens_skipped": sum(1 for s in opens if s.get("human") == "disapproved"),
        "manages_proposed": len(manages),
        "manages_executed": sum(1 for s in manages if s.get("human") == "approved"),
        "manages_declined": sum(1 for s in manages if s.get("human") == "disapproved"),
        "note": "Skipped-signal P&L needs outcomes for the shadow names; counts shown until those land.",
    }

    report = {
        "generated_at": now_iso(),
        "role": "performance-analyst (read-only reporter; does not steer weights or trade)",
        "window": {"from": curve[0]["date"] if curve else None,
                   "to": curve[-1]["date"] if curve else None, "graded_trades": n},
        "headline": {
            "total_return_pct": total_return,
            "sortino_net": _sortino(rets),
            "sharpe_net": _sharpe(rets),
            "max_drawdown_pct": max_dd,
            "hit_rate_pct": hit,
            "avg_win_pct": avg_win,
            "avg_loss_pct": avg_loss,
            "payoff_ratio": payoff,
            "cvar_25_pct": _cvar(rets),
            "worst_trade_pct": round(min(rets), 2) if rets else 0.0,
            "avg_cost_drag_pct": cost_drag,
        },
        "attribution_by_analyst": dict(sorted(analyst_pnl.items(), key=lambda kv: kv[1], reverse=True)),
        "attribution_by_ticker": dict(sorted(ticker_pnl.items(), key=lambda kv: kv[1], reverse=True)),
        "shadow_book": shadow,
        "equity_curve": curve,
    }
    (BENCH / "performance_report.json").write_text(json.dumps(report, indent=2))
    render_svg(curve, BENCH / "equity_curve.svg")

    h = report["headline"]
    print("PERFORMANCE REPORT (reporter — read-only; steering stays in benchmark.py)\n")
    print(f"Window {report['window']['from']} → {report['window']['to']}   graded trades: {n}")
    print(f"Total return {h['total_return_pct']:+.2f}%   max drawdown {h['max_drawdown_pct']:.2f}%")
    print(f"Sortino(net) {h['sortino_net']}   Sharpe(net) {h['sharpe_net']}   CVaR(25%) {h['cvar_25_pct']}%   worst {h['worst_trade_pct']}%")
    print(f"Hit rate {h['hit_rate_pct']}%   avg win {h['avg_win_pct']}%   avg loss {h['avg_loss_pct']}%   payoff {h['payoff_ratio']}   cost drag {h['avg_cost_drag_pct']}%/trade")
    print("\nEquity curve (index 100):")
    for c in curve:
        bar = "#" * max(0, int((c["equity"] - 100) // 1))
        print(f"  {c['date']}  {c['ticker']:<5} {c['ret_pct']:+6.2f}%  ->  {c['equity']:7.2f}  dd {c['drawdown_pct']:4.1f}%  {bar}")
    print("\nAttribution by analyst (summed net P&L when it voted):")
    for name, pnl in report["attribution_by_analyst"].items():
        print(f"  {name:<22}{pnl:+7.2f}")
    print("\nAttribution by ticker:")
    for tkr, pnl in report["attribution_by_ticker"].items():
        print(f"  {tkr:<6}{pnl:+7.2f}")
    print(f"\nLive vs shadow book: {shadow['opens_taken']} taken / {shadow['opens_skipped']} skipped "
          f"of {shadow['opens_proposed']} proposed; managed exits {shadow['manages_executed']} done / "
          f"{shadow['manages_declined']} declined.")
    print("\nWrote benchmarks/performance_report.json + benchmarks/equity_curve.svg  (no weights touched).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
