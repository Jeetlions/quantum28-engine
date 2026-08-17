#!/usr/bin/env python3
"""
Benchmark — the STEERING WHEEL of the feedback loop (the optimizer, not the reporter).

Where performance.py measures and never touches a weight, THIS file is the half that learns:
once a week it joins each logged decision to its realized outcome, scores every VOTER by how
its calls actually performed, and re-weights the vote for next week. Hermes model weights are
frozen, so an analyst can't get better on its own — this is how the fund gets better: by
trusting the voters that earn it and fading the ones that don't.

Objective: reward realized risk-adjusted return and PENALIZE drawdown — a per-analyst
    objective_i = Sharpe(its contribution stream)  -  LAMBDA * (its max drawdown fraction)
Scores are turned into BOUNDED, EWMA-smoothed weights so no single analyst can dominate and a
noisy week can't whipsaw the book.

Reads   benchmarks/decision_log(.seed).jsonl   what the framework decided (voters + stances)
        benchmarks/outcomes.jsonl              realized returns per decision_id
Writes  benchmarks/analyst_weights.json        {"weights": {analyst: w}}  <- orchestrator.load_weights()

Deterministic, pure standard library, places no trade and moves no money.  Run: python3 benchmark.py
"""
from __future__ import annotations
import json, math, re
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent          # this file lives inside benchmarks/
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
DEFAULT_COST_BPS = 10

# ---- knobs (documented heuristics — tune, don't overfit) --------------------
W_MIN, W_MAX = 0.5, 1.5     # weights stay bounded so no voter dominates the vote
LAMBDA_DD    = 0.5          # drawdown penalty in the per-analyst objective
GAIN         = 0.25         # how hard a good/bad z-score moves the weight off 1.0
SMOOTH       = 0.5          # EWMA blend with last week's weight (0=snap, 1=frozen)
MIN_OBS      = 4            # an analyst needs this many graded votes before we reweight it
MIN_TRADES   = 5           # need this many graded trades overall before trusting any reweight


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
    """Direction the framework took: the logged side if present, else the sign of the voters'
    net conviction (identical rule to performance.py, so reporter and optimizer never disagree)."""
    side = d.get("side")
    if side in ("long", "short", "neutral", "none", "flat"):
        return sign(side)
    net = sum(sign(c["stance"]) * float(c["conviction"]) for c in d.get("voters", {}).values())
    return 1 if net > 0 else (-1 if net < 0 else 0)


def _sharpe(rets: list[float]) -> float:
    n = len(rets)
    if n < 2:
        return 0.0
    mean = sum(rets) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (n - 1))
    return mean / sd if sd > 1e-9 else (3.0 if mean > 0 else 0.0)


def _max_dd_pct(rets: list[float]) -> float:
    """Max drawdown (%) of the equity built by compounding a per-trade return stream (in %)."""
    eq, peak, mdd = 100.0, 100.0, 0.0
    for r in rets:
        eq *= (1 + r / 100.0)
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak * 100.0)
    return mdd


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def main() -> int:
    decisions = {d["decision_id"]: d for d in read_jsonl(HERE / "decision_log.seed.jsonl")}
    decisions.update({d["decision_id"]: d for d in read_jsonl(HERE / "decision_log.jsonl")})
    outcomes = {o["decision_id"]: o for o in read_jsonl(HERE / "outcomes.jsonl")}
    prev = {}
    pf = HERE / "analyst_weights.json"
    if pf.exists():
        prev = json.loads(pf.read_text()).get("weights", {})

    # ---- grade trades and accumulate each voter's own contribution stream ----
    trades: list[dict] = []
    contrib: dict[str, list[float]] = {}
    for did, d in decisions.items():
        o = outcomes.get(did)
        if not o:
            continue
        dirn = direction(d)
        if dirn == 0:
            continue                                   # flat/neutral: no directional P&L to grade
        R = float(o["long_return_pct"])
        cost = float(o.get("cost_bps", DEFAULT_COST_BPS)) / 100.0
        trades.append({"date": dparse(did), "ret": dirn * R - cost})
        for name, call in d.get("voters", {}).items():
            s = sign(call["stance"])
            if s != 0:                                 # credit/blame the voter by ITS own call
                contrib.setdefault(name, []).append(s * R - cost)

    trades.sort(key=lambda t: t["date"])
    port_rets = [t["ret"] for t in trades]
    portfolio = {
        "graded_trades": len(port_rets),
        "sharpe": round(_sharpe(port_rets), 3),
        "max_drawdown_pct": round(_max_dd_pct(port_rets), 2),
        "total_return_pct": round((math.prod(1 + r / 100.0 for r in port_rets) - 1) * 100, 2) if port_rets else 0.0,
    }

    # ---- per-analyst objective = Sharpe - LAMBDA * drawdown ------------------
    obj, diag = {}, {}
    for name, rs in contrib.items():
        sh, dd = _sharpe(rs), _max_dd_pct(rs)
        o = sh - LAMBDA_DD * (dd / 100.0)
        diag[name] = {"votes": len(rs), "sharpe": round(sh, 3), "max_dd_pct": round(dd, 2),
                      "objective": round(o, 3)}
        if len(rs) >= MIN_OBS:
            obj[name] = o

    # ---- objective -> bounded, EWMA-smoothed weights ------------------------
    weights = {}
    enough = len(port_rets) >= MIN_TRADES
    if obj and enough:
        vals = list(obj.values())
        mean = sum(vals) / len(vals)
        sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals)) or 1.0
        for name in contrib:                            # every voter that appeared this window
            p = prev.get(name, 1.0)
            if name in obj:
                z = (obj[name] - mean) / sd
                proposed = _clamp(1.0 + GAIN * z, W_MIN, W_MAX)
            else:
                proposed = p                            # not enough obs yet -> hold its weight
            w = round(SMOOTH * p + (1 - SMOOTH) * proposed, 3)
            weights[name] = w
            if name in diag:
                diag[name]["weight"] = w
    else:
        # not enough evidence yet — keep everyone equal (or hold prior), don't over-fit noise
        for name in contrib:
            weights[name] = round(prev.get(name, 1.0), 3)
            if name in diag:
                diag[name]["weight"] = weights[name]

    out = {
        "generated_at": now_iso(),
        "role": "benchmark (optimizer/steering wheel; reweights the vote, never trades)",
        "objective": "per_analyst Sharpe - LAMBDA*max_drawdown, bounded + EWMA-smoothed",
        "params": {"w_min": W_MIN, "w_max": W_MAX, "lambda_dd": LAMBDA_DD, "gain": GAIN,
                   "smooth": SMOOTH, "min_obs": MIN_OBS, "min_trades": MIN_TRADES},
        "reweighted": bool(obj and enough),
        "portfolio": portfolio,
        "per_analyst": dict(sorted(diag.items(), key=lambda kv: kv[1]["objective"], reverse=True)),
        "weights": weights,                              # <- consumed by orchestrator.load_weights()
        "note": "Missing analyst => weight 1.0 in the orchestrator. Weekly cadence (Fri).",
    }
    HERE.mkdir(parents=True, exist_ok=True)
    (HERE / "analyst_weights.json").write_text(json.dumps(out, indent=2))

    print("BENCHMARK — weekly reweight (optimizer; no trade placed)\n")
    print(f"graded trades {portfolio['graded_trades']}   portfolio Sharpe {portfolio['sharpe']}   "
          f"maxDD {portfolio['max_drawdown_pct']}%   total {portfolio['total_return_pct']:+.2f}%")
    print(f"reweighted: {out['reweighted']}  (need >= {MIN_TRADES} trades and >= {MIN_OBS} votes/analyst)\n")
    for name, dg in out["per_analyst"].items():
        w = dg.get("weight", "-")
        print(f"  {name:<22} votes {dg['votes']:>3}  Sharpe {dg['sharpe']:>6}  "
              f"maxDD {dg['max_dd_pct']:>5}%  obj {dg['objective']:>6}  ->  w {w}")
    print("\nWrote benchmarks/analyst_weights.json — orchestrator reads it on the next vote.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
