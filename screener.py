#!/usr/bin/env python3
"""
screener.py — deterministic EOD candidate selection over the S&P 500 + Nasdaq 100.

The funnel's first stage: you can't run 13 reasoning bots on ~500 names (that's 6,500 calls). So
this ranks the WHOLE universe once a day on cheap, computed Tasty EOD signals and passes only the
top-N shortlist to the bots. Cheap wide screen -> deep narrow reasoning. No model calls.

It surfaces the most ACTIONABLE names — strong directional move (either way), volume behind it,
pressing a 52-week level, with enough liquidity to trade — and reports the trend bias; the bots
then decide long/short. Signals (all from Tasty daily candles):
  relative-strength momentum (20d/60d, ranked across the universe), volume surge, breakout/
  breakdown proximity to the 52-week range, volatility expansion. Liquidity (ADV) is a hard gate.

Universe: universe.txt (one ticker per line, '#' comments). Output: $QE_WORKSPACE/candidates.json.
Reuses quant_inputs for the Tasty candle fetch + indicator helpers. Pure standard library otherwise.
"""
from __future__ import annotations
import argparse, json, math, os
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE = Path(os.environ.get("QE_WORKSPACE", "/workspace"))
MIN_ADV_USD_M = float(os.environ.get("SCR_MIN_ADV_USD_M", "20"))     # liquidity gate: $20M/day default
WEIGHTS = {"momentum": 0.40, "volume": 0.25, "breakout": 0.20, "vol_expansion": 0.15}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def load_universe(path: str) -> list[str]:
    syms = []
    for ln in Path(path).read_text().splitlines():
        s = ln.split("#", 1)[0].strip().upper()
        if s:
            syms.append(s)
    # de-dupe, preserve order (S&P500 ∪ NDX100 overlap)
    seen, out = set(), []
    for s in syms:
        if s not in seen:
            seen.add(s); out.append(s)
    return out


def raw_metrics(symbol: str, candles: list[dict]) -> dict | None:
    """Per-name EOD signals from daily candles. None if too little history or illiquid."""
    rows = [c for c in candles if c.get("close") is not None]
    if len(rows) < 65:
        return None
    closes = [r["close"] for r in rows]
    highs = [r.get("high", r["close"]) for r in rows]
    lows = [r.get("low", r["close"]) for r in rows]
    vols = [r.get("volume", 0) or 0 for r in rows]
    px = closes[-1]
    ret_20 = px / closes[-21] - 1
    ret_60 = px / closes[-61] - 1
    adv_usd_m = sum(closes[i] * vols[i] for i in range(-20, 0)) / 20 / 1e6
    base_vol = sum(vols[-60:]) / 60 or 1
    vol_surge = (sum(vols[-5:]) / 5) / base_vol
    rets = [closes[i] / closes[i - 1] - 1 for i in range(-60, 0)]
    mean = sum(rets) / len(rets)
    ann_vol = (sum((x - mean) ** 2 for x in rets) / len(rets)) ** 0.5 * math.sqrt(252)
    short_vol = (sum((rets[i] - sum(rets[-10:]) / 10) ** 2 for i in range(-10, 0)) / 10) ** 0.5 * math.sqrt(252)
    hi_52, lo_52 = max(highs), min(lows)
    dist_high = px / hi_52 - 1        # ~0 at the high (breakout), negative below
    dist_low = px / lo_52 - 1         # ~0 at the low (breakdown), positive above
    near_level = min(abs(dist_high), abs(dist_low))
    return {"symbol": symbol.upper(), "px": round(px, 2), "ret_20": ret_20, "ret_60": ret_60,
            "adv_usd_m": round(adv_usd_m, 2), "vol_surge": round(vol_surge, 2),
            "ann_vol": round(ann_vol, 3), "vol_expansion": round(short_vol / ann_vol, 2) if ann_vol else 1.0,
            "dist_52w_high": round(dist_high, 3), "dist_52w_low": round(dist_low, 3),
            "near_level": round(near_level, 3)}


def _pct_rank(values: list[float]) -> dict:
    """Map each distinct value to its percentile [0,1] (ties share rank)."""
    order = sorted(values)
    n = len(order)
    return {v: (order.index(v) / (n - 1) if n > 1 else 0.5) for v in set(values)}


IVR_HIGH = float(os.environ.get("SCR_IVR_HIGH", "50"))      # above -> premium-selling regime
IVR_LOW = float(os.environ.get("SCR_IVR_LOW", "30"))        # below -> premium-buying regime


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def market_metrics(symbols: list[str]) -> dict:
    """Bulk IV rank / IV percentile / liquidity from Tasty's market-metrics endpoint (one call for the
    whole universe — cheap). {} on any failure (screener then runs price-only)."""
    try:
        from datafeed import TastytradeBackend
        from tastytrade.metrics import get_market_metrics
        be = TastytradeBackend()
        if not be.available():
            return {}
        out = {}
        for m in get_market_metrics(be._sess(), [s.upper() for s in symbols]):
            ivr = _f(getattr(m, "implied_volatility_index_rank", None)) or \
                  _f(getattr(m, "tos_implied_volatility_index_rank", None))
            if ivr is not None and ivr <= 1.0:
                ivr *= 100.0                          # Tasty returns 0-1; express as 0-100
            out[(getattr(m, "symbol", "") or "").upper()] = {
                "iv_rank": round(ivr, 1) if ivr is not None else None,
                "iv_pct": _f(getattr(m, "implied_volatility_percentile", None)),
            }
        return out
    except Exception:
        return {}


def options_enrich(symbol: str) -> dict:
    """Skew / term structure / put-call / call convexity for ONE name (shortlist only, to keep the
    per-name chain pulls few). {} on failure."""
    try:
        from datafeed import DataFeed
        o = DataFeed().options_block(symbol.upper()).get("options", {}) or {}
        return {k: o.get(k) for k in ("skew", "term_structure", "put_call", "call_convexity", "iv_rank")
                if o.get(k) is not None}
    except Exception:
        return {}


# options-structure families: each gets its single best-fit name daily, so the 30% book never goes hungry
FAMILY_DESC = {
    "iron_condor":  "neutral premium sale (high IV, range-bound)",
    "put_write":    "cash-secured put / put-credit (high IV, bullish)",
    "call_credit":  "bear call credit spread (high IV, bearish / rich call skew)",
    "calendar":     "calendar / diagonal (cheap IV or backwardation)",
    "backspread":   "ratio backspread (cheap IV, breakout)",
    "debit_spread": "vertical debit (cheap IV, directional)",
}


def options_fit(s: dict) -> dict:
    """Fit [0,1] of a scored name for each options family, from IV rank, skew, bias, and momentum."""
    ivr = s.get("iv_rank")
    ivr = ivr if isinstance(ivr, (int, float)) else 50.0          # unknown -> neutral
    hi = _clamp((ivr - IVR_HIGH) / (100 - IVR_HIGH)) if ivr >= IVR_HIGH else 0.0
    lo = _clamp((IVR_LOW - ivr) / IVR_LOW) if ivr <= IVR_LOW else 0.0
    mom = _clamp(abs(2 * s.get("rs_60", 0.5) - 1))                # directional strength
    rng = 1.0 - mom                                               # range-boundness
    brk = _clamp(1.0 - s.get("near_level", 1.0) / 0.10)
    backw = 1.0 if s.get("term_structure") == "backwardation" else 0.0
    up = 1.0 if s.get("bias") == "up" else 0.0
    down = 1.0 if s.get("bias") == "down" else 0.0
    conv = {"high": 1.0, "medium": 0.5, "low": 0.0}.get(s.get("call_convexity"), 0.3)
    return {
        "iron_condor":  round(hi * 0.6 + rng * 0.4, 3),
        "put_write":    round(hi * 0.6 + up * 0.4, 3),
        "call_credit":  round(hi * 0.5 + down * 0.3 + conv * 0.2, 3),
        "calendar":     round(lo * 0.5 + backw * 0.5, 3),
        "backspread":   round(lo * 0.5 + mom * 0.3 + brk * 0.2, 3),
        "debit_spread": round(lo * 0.6 + mom * 0.4, 3),
    }


def build_options_candidates(scored: list[dict]) -> dict:
    """The single best-fit name for each options family — guarantees every structure a candidate/day."""
    out = {}
    for fam, desc in FAMILY_DESC.items():
        best, bestfit = None, -1.0
        for s in scored:
            fit = options_fit(s)[fam]
            if fit > bestfit:
                best, bestfit = s, fit
        if best:
            out[fam] = {"symbol": best["symbol"], "structure": desc, "fit": round(bestfit, 3),
                        "iv_rank": best.get("iv_rank"), "bias": best.get("bias"), "px": best.get("px"),
                        "skew": best.get("skew"), "term_structure": best.get("term_structure"),
                        "rationale": f"{desc}: {best['symbol']} IVR {best.get('iv_rank', '?')}, "
                                     f"{best.get('bias')}-bias; {best.get('reason', '')}".strip()}
    return out


def screen(universe: list[str], top: int = 20, fetch=None, min_adv: float = MIN_ADV_USD_M,
           iv_metrics: dict | None = None, enrich_fn=None) -> dict:
    """Fetch EOD candles for the universe, score, and return the top-N shortlist (+ full ranking)."""
    if fetch is None:
        import quant_inputs
        fetch = quant_inputs.tasty_candles

    raws, errors = [], 0
    for sym in universe:
        try:
            m = raw_metrics(sym, fetch(sym))
            if m and m["adv_usd_m"] >= min_adv:       # liquidity gate
                raws.append(m)
        except Exception:
            errors += 1

    if not raws:
        return {"as_of": now_iso(), "screened": 0, "errors": errors, "candidates": [], "ranked": []}

    # cross-sectional relative strength (percentile of momentum across the universe)
    r20 = _pct_rank([m["ret_20"] for m in raws])
    r60 = _pct_rank([m["ret_60"] for m in raws])
    scored = []
    for m in raws:
        rs20, rs60 = r20[m["ret_20"]], r60[m["ret_60"]]
        momentum = _clamp((abs(2 * rs60 - 1) * 0.6 + abs(2 * rs20 - 1) * 0.4))     # extremity of relative move
        volume = _clamp((m["vol_surge"] - 1.0) / 1.0)                              # >1 = surge
        breakout = _clamp(1.0 - m["near_level"] / 0.10)                            # within 10% of a 52w extreme
        vol_exp = _clamp((m["vol_expansion"] - 1.0) / 0.5)                         # recent vol vs its own baseline
        score = 100 * (WEIGHTS["momentum"] * momentum + WEIGHTS["volume"] * volume
                       + WEIGHTS["breakout"] * breakout + WEIGHTS["vol_expansion"] * vol_exp)
        bias = "up" if rs60 >= 0.5 else "down"
        near = "52w-high" if abs(m["dist_52w_high"]) <= abs(m["dist_52w_low"]) else "52w-low"
        reasons = []
        if momentum > 0.5: reasons.append(f"{'strong' if momentum>0.75 else 'notable'} {bias}-momentum (RS60 {rs60:.2f})")
        if volume > 0.3: reasons.append(f"volume surge {m['vol_surge']:.1f}x")
        if breakout > 0.3: reasons.append(f"near {near} ({m['near_level']*100:.1f}%)")
        if vol_exp > 0.3: reasons.append("volatility expanding")
        scored.append({**m, "score": round(score, 1), "bias": bias,
                       "rs_20": round(rs20, 2), "rs_60": round(rs60, 2),
                       "reason": "; ".join(reasons) or "mild"})

    scored.sort(key=lambda x: x["score"], reverse=True)

    # --- vol layer: bulk IV rank for all; skew/term for the richest-IV names; then structure buckets ---
    ivm = iv_metrics if iv_metrics is not None else market_metrics([s["symbol"] for s in scored])
    for s in scored:
        info = ivm.get(s["symbol"], {})
        if info.get("iv_rank") is not None:
            s["iv_rank"] = info["iv_rank"]
    enrich = enrich_fn if enrich_fn is not None else options_enrich
    for s in sorted(scored, key=lambda x: (x.get("iv_rank") or 0), reverse=True)[:25]:
        for k, v in (enrich(s["symbol"]) or {}).items():
            if v is not None:
                s[k] = v
    options_candidates = build_options_candidates(scored)

    shortlist = [{"symbol": s["symbol"], "score": s["score"], "bias": s["bias"], "px": s["px"],
                  "adv_usd_m": s["adv_usd_m"], "iv_rank": s.get("iv_rank"), "reason": s["reason"]}
                 for s in scored[:top]]
    return {"as_of": now_iso(), "universe": len(universe), "screened": len(scored),
            "errors": errors, "top": top, "candidates": shortlist,
            "options_candidates": options_candidates, "ranked": scored}


def main() -> int:
    ap = argparse.ArgumentParser(description="EOD screener over S&P500 + NDX100 -> daily shortlist.")
    ap.add_argument("--universe", default=str(Path(__file__).resolve().parent / "universe.txt"))
    ap.add_argument("--top", type=int, default=int(os.environ.get("SCR_TOP", "20")))
    ap.add_argument("--min-adv", type=float, default=MIN_ADV_USD_M, help="liquidity gate, $M/day")
    ap.add_argument("--out", default=str(WORKSPACE / "candidates.json"))
    a = ap.parse_args()

    universe = load_universe(a.universe)
    res = screen(universe, top=a.top, min_adv=a.min_adv)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=2))

    print(f"[screener] universe {res.get('universe', len(universe))} · screened {res['screened']} · "
          f"errors {res['errors']} · top {res.get('top', a.top)} -> {a.out}")
    for i, c in enumerate(res["candidates"], 1):
        ivr = c.get("iv_rank")
        print(f"  {i:>2}. {c['symbol']:<6} score {c['score']:>5}  {c['bias']:<4} ${c['px']:<8} "
              f"ADV ${c['adv_usd_m']}M  IVR {ivr if ivr is not None else '-'}  · {c['reason']}")
    oc = res.get("options_candidates", {})
    if oc:
        print("  options book — one candidate per structure:")
        for fam, c in oc.items():
            print(f"    {fam:<13} {c['symbol']:<6} IVR {c.get('iv_rank', '?')}  {c.get('bias', ''):<4} · {c['structure']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
