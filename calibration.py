#!/usr/bin/env python3
"""
Entry calibration loop — tune the ENTRY parameters of rule-based strategies from their own
realized outcomes. Hermes model weights are frozen, so an agent does not learn better entries on
its own; this closes that loop for the deterministic rules the way benchmark.py closes it for the
vote. It is general: put-writes today, verticals today, and any future rule that logs an outcome.

Reads  benchmarks/entry_outcomes.jsonl  (one record per closed entry) and writes
       benchmarks/calibration.json       (tuned params the orchestrator/strategist consume next run).

It tunes, it does not trade and does not touch analyst_weights.json.

Record shapes (illustrative):
  put_write : {"strategy":"put_write","ticker":"AAPL","iv_rank":58,"assigned":false,
               "breach_vs_floor_pct":-3.1,"return_pct":1.2}
  vertical  : {"strategy":"vertical","substrategy":"vertical_credit","ticker":"MSFT",
               "iv_rank":62,"win":true,"return_pct":0.8}

Pure standard library.  Run:  python3 calibration.py
"""
from __future__ import annotations
import json, statistics
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
BENCH = BASE / "benchmarks"

# put-write tuning bands / steps
PW_TARGET_ASSIGN_LO, PW_TARGET_ASSIGN_HI = 0.10, 0.20   # desired assignment-rate band
PW_BUFFER_MIN, PW_BUFFER_MAX, PW_BUFFER_STEP = 0.01, 0.06, 0.005
PW_BUFFER_DEFAULT = 0.02
PW_IVR_DEFAULT = 40
# vertical tuning
V_IVR_PIVOT_DEFAULT = 50
MIN_N = 4                       # need at least this many trades before we move a knob
SUIT_MIN, SUIT_MAX = 0.3, 1.5   # per-name suitability multiplier bounds


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _suitability(rows, key_win):
    """Per-name multiplier: reward names that worked, penalize/blacklist repeat offenders."""
    by = {}
    for r in rows:
        by.setdefault(r["ticker"], []).append(r)
    out = {}
    for tkr, rs in by.items():
        wins = sum(1 for r in rs if key_win(r))
        rate = wins / len(rs)
        mult = round(max(SUIT_MIN, min(SUIT_MAX, 0.5 + rate)), 2)   # 0% wins->0.5x .. 100%->1.5x
        rec = {"n": len(rs), "win_rate": round(rate, 2), "multiplier": mult}
        if len(rs) >= MIN_N and rate <= 0.25:
            rec["avoid"] = True                                     # blacklist repeat losers
        out[tkr] = rec
    return out


def calibrate_put_write(rows):
    if not rows:
        return {"strike_buffer_pct": PW_BUFFER_DEFAULT, "min_iv_rank": PW_IVR_DEFAULT,
                "name_suitability": {}, "note": "no put-write outcomes yet — defaults"}
    n = len(rows)
    assign_rate = sum(1 for r in rows if r.get("assigned")) / n

    # buffer: too many assignments -> widen the buffer; too few -> tighten (stop leaving premium)
    buf = PW_BUFFER_DEFAULT
    prev = read_jsonl(BENCH / "calibration.json") if False else None  # (history not needed; recompute)
    if n >= MIN_N:
        if assign_rate > PW_TARGET_ASSIGN_HI:
            buf = PW_BUFFER_DEFAULT + PW_BUFFER_STEP * 2
        elif assign_rate < PW_TARGET_ASSIGN_LO:
            buf = PW_BUFFER_DEFAULT - PW_BUFFER_STEP
        # data-driven refinement: cover the 85th percentile of adverse breaches vs the floor
        breaches = sorted(-float(r["breach_vs_floor_pct"]) / 100.0 for r in rows
                          if r.get("breach_vs_floor_pct") is not None and float(r["breach_vs_floor_pct"]) < 0)
        if breaches:
            k = max(0, int(0.85 * (len(breaches) - 1)))
            buf = max(buf, round(breaches[k], 3))
    buf = round(max(PW_BUFFER_MIN, min(PW_BUFFER_MAX, buf)), 3)

    # min IV rank: lowest iv_rank bucket whose mean net return is positive (only write when vol pays)
    floor = PW_IVR_DEFAULT
    buckets = {}
    for r in rows:
        b = int(float(r.get("iv_rank", 0)) // 10) * 10
        buckets.setdefault(b, []).append(float(r.get("return_pct", 0)))
    good = sorted(b for b, rr in buckets.items() if statistics.mean(rr) > 0)
    if good:
        floor = good[0]

    return {"strike_buffer_pct": buf, "min_iv_rank": floor,
            "assignment_rate": round(assign_rate, 2), "n": n,
            "name_suitability": _suitability(rows, lambda r: not r.get("assigned")),
            "note": f"buffer {buf*100:.1f}% below lowest floor; write only when IV rank >= {floor}"}


def _tune_param(rows, key, default, lo, hi, round_to=3):
    """Evidence-based knob: place the parameter where the WINNERS were placed (mean of the param over
    positive-return trades), clamped to [lo, hi]. Needs MIN_N samples or it keeps the default."""
    vals = [r for r in rows if r.get(key) is not None and r.get("return_pct") is not None]
    if len(vals) < MIN_N:
        return default
    winners = [float(r[key]) for r in vals if float(r["return_pct"]) > 0]
    val = statistics.mean(winners) if winners else default
    return round(max(lo, min(hi, val)), round_to)


def calibrate_butterfly(rows):
    if not rows:
        return {"center_offset_pct": 0.0, "wing_width_em_mult": 1.0, "name_suitability": {},
                "note": "no butterfly outcomes yet — defaults (ATM/pin, wings = expected move)"}
    return {"center_offset_pct": _tune_param(rows, "center_offset_pct", 0.0, 0.0, 0.10),
            "wing_width_em_mult": _tune_param(rows, "wing_width_em_mult", 1.0, 0.5, 2.0, 2),
            "n": len(rows), "name_suitability": _suitability(rows, lambda r: r.get("win")),
            "note": "center offset (OTM=directional) and wing width as x expected-move, from winners"}


def calibrate_diagonal(rows):
    if not rows:
        return {"short_strike_dist_pct": 0.0, "tenor_gap_days": 35, "name_suitability": {},
                "note": "no diagonal outcomes yet — defaults (short at resistance/support, 35d gap)"}
    return {"short_strike_dist_pct": _tune_param(rows, "short_strike_dist_pct", 0.0, 0.0, 0.15),
            "tenor_gap_days": int(_tune_param(rows, "tenor_gap_days", 35, 14, 90, 0)),
            "n": len(rows), "name_suitability": _suitability(rows, lambda r: r.get("win")),
            "note": "how far beyond resistance/support to sell the front, and the back-month gap"}


def calibrate_calendar(rows):
    if not rows:
        return {"strike_offset_pct": 0.0, "tenor_gap_days": 35, "name_suitability": {},
                "note": "no calendar outcomes yet — defaults (ATM/neutral, 35d gap)"}
    return {"strike_offset_pct": _tune_param(rows, "strike_offset_pct", 0.0, 0.0, 0.08),
            "tenor_gap_days": int(_tune_param(rows, "tenor_gap_days", 35, 14, 90, 0)),
            "n": len(rows), "name_suitability": _suitability(rows, lambda r: r.get("win")),
            "note": "strike offset (0=ATM/neutral, >0=OTM/directional toward drift) and the tenor gap"}


def calibrate_vertical(rows):
    if not rows:
        return {"iv_rank_pivot": V_IVR_PIVOT_DEFAULT, "name_suitability": {},
                "note": "no vertical outcomes yet — defaults"}
    # find the IV-rank pivot where CREDIT starts beating DEBIT (sell premium above it, buy below)
    pivot = V_IVR_PIVOT_DEFAULT
    best = None
    for cand in range(20, 81, 5):
        credit = [float(r["return_pct"]) for r in rows if float(r.get("iv_rank", 0)) >= cand and r.get("substrategy") == "vertical_credit"]
        debit = [float(r["return_pct"]) for r in rows if float(r.get("iv_rank", 0)) < cand and r.get("substrategy") == "vertical_debit"]
        if len(credit) + len(debit) < MIN_N:
            continue
        score = (statistics.mean(credit) if credit else 0) + (statistics.mean(debit) if debit else 0)
        if best is None or score > best[0]:
            best = (score, cand)
    if best:
        pivot = best[1]
    return {"iv_rank_pivot": pivot, "n": len(rows),
            "name_suitability": _suitability(rows, lambda r: r.get("win")),
            "note": f"sell premium (credit) when IV rank >= {pivot}; buy (debit) below"}


def main():
    rows = read_jsonl(BENCH / "entry_outcomes.jsonl")
    by = lambda s: [r for r in rows if r.get("strategy") == s]
    calib = {"generated_at": now_iso(),
             "put_write": calibrate_put_write(by("put_write")),
             "vertical": calibrate_vertical(by("vertical")),
             "butterfly": calibrate_butterfly(by("butterfly")),
             "diagonal": calibrate_diagonal(by("diagonal")),
             "calendar": calibrate_calendar(by("calendar"))}
    (BENCH / "calibration.json").write_text(json.dumps(calib, indent=2))

    print("ENTRY CALIBRATION (tunes rule parameters from realized outcomes — does not trade)\n")
    p = calib["put_write"]
    print(f"put_write  n={p.get('n',0)}  assignment_rate={p.get('assignment_rate','-')}")
    print(f"           strike_buffer -> {p['strike_buffer_pct']*100:.1f}% below lowest floor")
    print(f"           min_iv_rank   -> {p['min_iv_rank']}  (write only when IV is rich enough)")
    avoid = [t for t, s in p.get("name_suitability", {}).items() if s.get("avoid")]
    if avoid:
        print(f"           AVOID names   -> {', '.join(avoid)}")
    v = calib["vertical"]
    print(f"vertical   n={v.get('n',0)}  iv_rank_pivot -> {v['iv_rank_pivot']}  (credit above / debit below)")
    b = calib["butterfly"]
    print(f"butterfly  n={b.get('n',0)}  center_offset -> {b['center_offset_pct']*100:.1f}% (OTM=directional)  "
          f"wing_width -> {b['wing_width_em_mult']}x expected move")
    dg = calib["diagonal"]
    print(f"diagonal   n={dg.get('n',0)}  short_strike_dist -> {dg['short_strike_dist_pct']*100:.1f}% beyond level  "
          f"tenor_gap -> {dg['tenor_gap_days']}d")
    cl = calib["calendar"]
    print(f"calendar   n={cl.get('n',0)}  strike_offset -> {cl['strike_offset_pct']*100:.1f}% "
          f"({'OTM/directional' if cl['strike_offset_pct'] > 0 else 'ATM/neutral'})  tenor_gap -> {cl['tenor_gap_days']}d")
    print("\nWrote benchmarks/calibration.json — orchestrator & strategist read it next run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
