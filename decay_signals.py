#!/usr/bin/env python3
"""
decay_signals.py — signal generator for the levered-ETF decay sleeve.

Implements the signal architecture from "The Decay Edge" (Chapters 5, 7, 8, appendix C) exactly:
a THREE-LEVEL hierarchy, per-instrument-class weights, 0–100 signal scores, a Level-3 options
multiplier, and the 80/65/50 sizing thresholds. Output is a daily dashboard of SHORT setups on
levered / inverse / vol ETFs, plus proposals the CIO sleeve (cio.py --sleeve levered_etf) ingests.

Hierarchy (book 5.1):
  Level 1  regime      — is the instrument in a decay-favorable regime at all?
  Level 2  timing      — is the timing within that regime good?
  Level 3  sentiment   — options-market extremity that SHARPENS the trigger (a multiplier)

Composite (book appendix C.2/C.6):
  base       = sum(signal_score × weight)            # each score 0–100, weights sum to 1
  L3 mult    = 1.0 + 0.5 × max(0, (conv_z − 1.5))    # capped at 1.5
  composite  = min(100, base × L3 mult)

Sizing (book 5.6):  ≥80 full (2%) · 65–79 half (1%) · 50–64 quarter (0.5%) · <50 skip.
Hard stand-aside:   curve in BACKWARDATION → no new shorts (book 5.2 / 5.3).

Structures are DEFINED-RISK only (sleeve rule): bear put debit spread (delta-25/delta-10), a
broken-wing put fly for a targeted reversion (double-decay, ch 6.3), or a bear call credit spread.

Everything is a documented heuristic per the book ("weights matter less than consistency").
Pure standard library.
"""
from __future__ import annotations
import json, os
from datetime import datetime, timezone
from pathlib import Path


def _load_env():
    """Minimal .env loader so a LOCAL run sees EIA_API_KEY etc. (on Railway the vars are already set)."""
    try:
        for line in (Path(__file__).resolve().parent / ".env").read_text().splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_env()

# ---- instrument classes & per-class weights ---------------------------------
# vol / natgas / single-stock are the book's classes (5.6 / C.6) verbatim. crude / metals /
# lev-index are EXTENSIONS built on the same 3-level template and the same mechanics (curve/
# inventory for futures-based ETFs; vol-drag + mean-reversion for the levered ones) — documented
# heuristics, not from the book. All framed so higher = more favorable for a SHORT.
VOL = {"UVXY", "VXX", "UVIX", "SVXY", "VIXY"}
NATGAS = {"BOIL", "KOLD", "UNG", "UGAZ", "DGAZ"}
CRUDE = {"UCO", "SCO", "USO", "DWT", "UWT", "GUSH", "DRIP", "OILU", "OILD"}
METALS = {"NUGT", "DUST", "JNUG", "JDST", "AGQ", "ZSL", "USLV", "DSLV", "UGL", "GLL", "GDXU", "GDXD"}
LEV_INDEX = {"TQQQ", "SQQQ", "SPXL", "SPXS", "UPRO", "SPXU", "TNA", "TZA", "SOXL", "SOXS", "UDOW", "SDOW", "QID", "QLD"}
SINGLE_STOCK_LEV = {"NVDL", "TSLL", "MSTU", "CONL", "AMDL", "MSFU", "GGLL", "TSLQ", "NVD"}

WEIGHTS = {
    "vol":       {"vx_term": 0.35, "vix_z": 0.25, "vix_rsi": 0.15, "season": 0.10, "rv_iv": 0.15},
    "natgas":    {"contango": 0.30, "storage": 0.25, "weather": 0.20, "season": 0.10, "rv": 0.10, "iv": 0.05},
    "crude":     {"contango": 0.35, "inventory": 0.25, "season": 0.10, "rv": 0.15, "iv": 0.15},
    "metals":    {"rv_iv": 0.30, "mean_rev": 0.25, "macro_tilt": 0.15, "rv": 0.15, "iv": 0.15},
    "lev_index": {"rv_iv": 0.30, "tech_setup": 0.30, "pcr_z": 0.15, "convexity": 0.15, "iv": 0.10},
    "single":    {"rv_iv": 0.25, "tech_setup": 0.25, "er_cycle": 0.20, "pcr_z": 0.15, "convexity": 0.15},
}
SIZE = [(80, "full", 0.020), (65, "half", 0.010), (50, "quarter", 0.005)]   # book 5.6 / C.7


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def instrument_class(ticker: str) -> str:
    t = ticker.upper()
    if t in VOL:
        return "vol"
    if t in NATGAS:
        return "natgas"
    if t in CRUDE:
        return "crude"
    if t in METALS:
        return "metals"
    if t in LEV_INDEX:
        return "lev_index"
    return "single"   # default: levered single-name treatment


# =============================================================================
# 0–100 signal scorers — each encodes the book's stated thresholds.
# All framed so HIGHER = more favorable for a SHORT (decay-capture) trade.
# =============================================================================
def score_vx_term(vx1_vx2: float):
    """VIX term structure VX1/VX2 (book 5.2): ≤0.93 steep contango (optimal); >1.00 backwardation STOP."""
    stand_aside = vx1_vx2 > 1.00
    if vx1_vx2 <= 0.90:
        s = 100.0
    elif vx1_vx2 >= 1.00:
        s = 0.0
    else:
        s = (1.00 - vx1_vx2) / 0.10 * 100.0     # linear 0.90→100 .. 1.00→0
    return _clamp(s), stand_aside


def score_contango(ng1_ng2: float):
    """Nat-gas front/second futures (book 5.3): ≤0.95 contango favorable; >1.00 backwardation STOP."""
    stand_aside = ng1_ng2 > 1.00
    if ng1_ng2 <= 0.93:
        s = 100.0
    elif ng1_ng2 >= 1.00:
        s = 0.0
    else:
        s = (1.00 - ng1_ng2) / 0.07 * 100.0
    return _clamp(s), stand_aside


def score_storage(surplus_pct: float):
    """Storage vs 5-yr avg (book 5.3): >+5% oversupply (bullish shorts); <−5% tight (bad)."""
    return _clamp((surplus_pct + 5.0) / 10.0 * 100.0)


def score_weather(favorability: float):
    """6–15 day demand read in [0,1]: 1 = normal/favorable (no extreme demand), 0 = extreme demand."""
    return _clamp(favorability * 100.0)


def score_season(ticker_class: str, month: int):
    """Seasonality (book 5.4) — a tiebreaker, not a primary signal."""
    if ticker_class == "natgas":                 # peak short window late Apr–Aug, best mid-May–Jul
        table = {1: 25, 2: 25, 3: 40, 4: 60, 5: 85, 6: 100, 7: 95, 8: 80, 9: 55, 10: 35, 11: 30, 12: 20}
    elif ticker_class == "vol":                  # short-vol edge late Jul & late Nov; Sep–Oct poor
        table = {1: 55, 2: 55, 3: 55, 4: 55, 5: 55, 6: 60, 7: 80, 8: 65, 9: 30, 10: 35, 11: 80, 12: 60}
    else:
        return 50.0
    return float(table.get(month, 50))


def score_vix_z(z: float):
    """VIX Z-score vs 60/120-day mean (book 5.5): ≥+2.0 strong short; ≤−1.0 stand aside."""
    return _clamp((z + 1.0) / 3.0 * 100.0)       # −1→0, +2→100


def score_vix_rsi(rsi: float):
    """VIX RSI (book 5.5): >70 overbought vol (short); <30 oversold (caution)."""
    return _clamp((rsi - 30.0) / 40.0 * 100.0)   # 30→0, 70→100


def score_rv_iv(ratio: float):
    """Realized/Implied (book 5.5 / 6.2): RV>IV (>1.2) => high beta slippage, favorable."""
    return _clamp((ratio - 0.8) / 0.7 * 100.0)   # 0.8→0, 1.5→100


def score_realized_calm(rv_ratio: float):
    """Recent realized vs 60-day avg (book 5.3 checklist): ≤1.2× = no spike (favorable)."""
    if rv_ratio <= 1.0:
        return 100.0
    if rv_ratio >= 1.5:
        return 0.0
    return _clamp((1.5 - rv_ratio) / 0.5 * 100.0)


def score_iv_band(iv_pct: float, lo=60.0, hi=120.0):
    """IV level tradeable band (book 5.3): inside [60,120]% good; outside taper."""
    if lo <= iv_pct <= hi:
        return 100.0
    if iv_pct < lo:
        return _clamp(iv_pct / lo * 100.0)
    return _clamp((1 - (iv_pct - hi) / hi) * 100.0)


def score_tech_setup(dist_above_200sma_pct: float, weekly_rsi: float, bb_pos: float):
    """Underlying technical exhaustion (book 6.2): far above 200-SMA, high weekly RSI, riding upper band."""
    d = _clamp(dist_above_200sma_pct / 50.0 * 100.0)          # +50% over 200-SMA → 100
    r = _clamp((weekly_rsi - 50.0) / 25.0 * 100.0)            # 50→0, 75→100
    b = _clamp(bb_pos * 100.0)                                 # 0..1 band position
    return round((d + r + b) / 3.0, 1)


def score_er_cycle(days_since_earnings):
    """IV cycle around earnings (book 7.4): T+15..T+45 optimal; T−7..T+1 worst."""
    if days_since_earnings is None:
        return 50.0
    d = days_since_earnings
    if 15 <= d <= 45:
        return 100.0                      # optimal entry window
    if 1 <= d < 15:
        return 70.0                       # post-crush, drift starting
    if -7 <= d <= 1:
        return 0.0                        # earnings danger window
    if -30 <= d < -7:
        return 30.0                       # IV ramp (paying up)
    return 50.0


def score_pcr_z(z: float):
    """Smart PCR Z-score (book 7.1): ≤−1.5 strong short (bullish crowd); >+0.5 reduces edge."""
    return _clamp((0.5 - z) / 2.0 * 100.0)        # z=−1.5→100, z=+0.5→0


def score_convexity(conv_z: float):
    """Call convexity Z-score (book 7.3): ≥+2.5 extreme; +1.5..+2.5 ramps; <+1.5 no signal."""
    if conv_z >= 2.5:
        return 100.0
    if conv_z <= 1.5:
        return 0.0
    return _clamp((conv_z - 1.5) / 1.0 * 100.0)


# ---- EXTENSION scorers (crude / metals / lev-index) — same mechanics, not from the book ------
def score_inventory(surplus_pct: float):
    """Crude/product inventory vs 5-yr avg (EIA): >+5% oversupplied (bearish → good short); <−5% tight."""
    return score_storage(surplus_pct)      # identical shape to nat-gas storage


def score_mean_rev(z: float):
    """Generic underlying Z-score vs its moving average: ≥+2.0 stretched high (short); ≤−1.0 stand aside."""
    return score_vix_z(z)                    # same mapping as VIX Z


def score_macro_tilt(favorability: float):
    """Metals macro tilt in [0,1]: 1 = backdrop favors the short (e.g. rising real yields / firm USD)."""
    return _clamp(favorability * 100.0)


# =============================================================================
# Composite + sizing + structure selection
# =============================================================================
def _score_all(ticker: str, m: dict):
    """Score whichever signals the class needs; returns {sig: (score, weight)} and stand_aside."""
    cls = instrument_class(ticker)
    w = WEIGHTS[cls]
    month = m.get("month") or datetime.now(timezone.utc).month
    stand_aside, reasons = False, []
    S = {}
    if cls == "vol":
        s, sa = score_vx_term(m["vx1_vx2"]); S["vx_term"] = s; stand_aside |= sa
        if sa: reasons.append(f"VX1/VX2 {m['vx1_vx2']} in backwardation")
        S["vix_z"] = score_vix_z(m["vix_z"]); S["vix_rsi"] = score_vix_rsi(m["vix_rsi"])
        S["season"] = score_season("vol", month); S["rv_iv"] = score_rv_iv(m["rv_iv"])
    elif cls == "natgas":
        s, sa = score_contango(m["ng1_ng2"]); S["contango"] = s; stand_aside |= sa
        if sa: reasons.append(f"NG1/NG2 {m['ng1_ng2']} in backwardation")
        S["storage"] = score_storage(m["storage_surplus_pct"]); S["weather"] = score_weather(m["weather_fav"])
        S["season"] = score_season("natgas", month); S["rv"] = score_realized_calm(m["rv_ratio"])
        S["iv"] = score_iv_band(m["iv_pct"])
    elif cls == "crude":
        s, sa = score_contango(m["cl1_cl2"]); S["contango"] = s; stand_aside |= sa
        if sa: reasons.append(f"CL1/CL2 {m['cl1_cl2']} in backwardation")
        S["inventory"] = score_inventory(m["inventory_surplus_pct"]); S["season"] = score_season("natgas", month)
        S["rv"] = score_realized_calm(m["rv_ratio"]); S["iv"] = score_iv_band(m["iv_pct"])
    elif cls == "metals":
        S["rv_iv"] = score_rv_iv(m["rv_iv"]); S["mean_rev"] = score_mean_rev(m["underlying_z"])
        S["macro_tilt"] = score_macro_tilt(m["macro_tilt"]); S["rv"] = score_realized_calm(m["rv_ratio"])
        S["iv"] = score_iv_band(m["iv_pct"])
    elif cls == "lev_index":
        S["rv_iv"] = score_rv_iv(m["rv_iv"])
        S["tech_setup"] = score_tech_setup(m["dist_above_200sma_pct"], m["weekly_rsi"], m["bb_pos"])
        S["pcr_z"] = score_pcr_z(m["pcr_z"]); S["convexity"] = score_convexity(m["convexity_z"])
        S["iv"] = score_iv_band(m["iv_pct"])
    else:   # single-stock levered
        S["rv_iv"] = score_rv_iv(m["rv_iv"])
        S["tech_setup"] = score_tech_setup(m["dist_above_200sma_pct"], m["weekly_rsi"], m["bb_pos"])
        S["er_cycle"] = score_er_cycle(m.get("days_since_earnings"))
        S["pcr_z"] = score_pcr_z(m["pcr_z"]); S["convexity"] = score_convexity(m["convexity_z"])
    return cls, {k: (S[k], w[k]) for k in w}, stand_aside, reasons


def _structure_hint(cls, composite, m):
    """Defined-risk short structure (book ch3 / 6.3)."""
    conv_z = m.get("convexity_z", 0)
    double_decay = cls == "single" and conv_z >= 2.0 and m.get("dist_above_200sma_pct", 0) >= 60
    if double_decay:
        return {"structure": "broken_wing_put_fly", "delta": {"long": 0.20, "short": 0.10, "far": 0.05},
                "note": "double-decay: stock exhaustion + ETF structural decay; fly captures the reversion zone"}
    if composite >= 80:
        return {"structure": "bear_put_debit_spread", "delta": {"long": 0.25, "short": 0.10},
                "note": "delta-25/delta-10 bear put spread; defined risk = debit"}
    return {"structure": "bear_call_credit_spread", "delta": {"short": 0.20, "long": 0.10},
            "note": "sell premium above; defined-risk credit spread when the down-move is lower conviction"}


# ---- live futures curves: FREE sources first, IB Gateway only as fallback ---
# The engine needs three front/second ratios — VX1/VX2 (vol), NG1/NG2 (nat gas), CL1/CL2 (crude).
# Primary sources are FREE and run anywhere (no IB Gateway, no 2FA, no VPS): EIA for the nat-gas and
# crude NYMEX settlement curves (your EIA_API_KEY), and the VIX / VIX3M index ratio (Cboe) as the vol
# term-structure proxy. IB Gateway is consulted only for whatever a free source couldn't return.
_CURVES = None
_CURVES_SRC = {}
CURVE_KEY_BY_CLASS = {"vol": "vx1_vx2", "natgas": "ng1_ng2", "crude": "cl1_cl2"}
_CURVE_ROOTS = {"vx1_vx2": ("VIX", "CFE"), "ng1_ng2": ("NG", "NYMEX"), "cl1_cl2": ("CL", "NYMEX")}


def _get(url, **kw):
    import requests
    r = requests.get(url, timeout=int(os.environ.get("HTTP_TIMEOUT", "15")),
                     headers={"User-Agent": "Quantum28/1.0"}, **kw)
    r.raise_for_status()
    return r


_EIA_ROUTE = {"RNGC1": "natural-gas/pri/fut", "RNGC2": "natural-gas/pri/fut",
              "RCLC1": "petroleum/pri/fut", "RCLC2": "petroleum/pri/fut"}


def _eia_series(series):
    """Latest daily settlement for an EIA futures series (RNGC1/2 = Henry Hub nat gas, RCLC1/2 = WTI)
    via the route+facet query — this pins the exact series and sorts newest-first, unlike the loose
    /seriesid/ route which mixes delivery areas. Returns None on no key / no data."""
    key = os.environ.get("EIA_API_KEY")
    route = _EIA_ROUTE.get(series)
    if not key or not route:
        return None
    try:
        d = _get(f"https://api.eia.gov/v2/{route}/data/",
                 params={"api_key": key, "frequency": "daily", "data[0]": "value",
                         "facets[series][]": series, "sort[0][column]": "period",
                         "sort[0][direction]": "desc", "length": 1}).json()
        rows = d.get("response", {}).get("data", [])
        if rows and rows[0].get("value") not in (None, ""):
            return float(rows[0]["value"])
    except Exception:
        pass
    return None


def _eia_ratio(s1, s2):
    a, b = _eia_series(s1), _eia_series(s2)
    return round(a / b, 4) if (a and b) else None


def _index_last(sym):
    """Latest index level from a free source: stooq CSV first, Yahoo chart as fallback."""
    try:
        row = _get(f"https://stooq.com/q/l/?s={sym}&f=sl1&e=csv").text.strip().splitlines()[-1]
        val = row.split(",")[-1]
        if val not in ("N/D", ""):
            return float(val)
    except Exception:
        pass
    try:
        j = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym.upper()}").json()
        return float(j["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except Exception:
        return None


def _vix_term():
    """VX1/VX2 proxy from the free VIX / VIX3M spot term structure (keyless, well-known). <1 = contango.
    VIX/VIX3M spans 1m→3m — about twice the 1m→2m span of the real VX1/VX2 futures — and the term
    structure is ~log-linear, so VX1/VX2 ≈ sqrt(VIX/VIX3M). Rescaling here keeps the book's VX1/VX2
    thresholds (≤0.90 optimal … >1.00 STOP) honest instead of over-reading the steeper 3-month curve
    (e.g. raw VIX/VIX3M 0.82 → 0.906, favorable-but-not-pegged, not a false max-conviction 100)."""
    vix, vix3m = _index_last("^vix"), _index_last("^vix3m")
    if not (vix and vix3m):
        return None
    return round((vix / vix3m) ** 0.5, 4)


def fetch_ib_curves() -> dict:
    """VIX / NG / CL front-second ratios from IB Gateway — the FALLBACK source (needs IB_ENABLED=1)."""
    out = {}
    try:
        from datafeed import IBKRBackend
        be = IBKRBackend()
        if not be.available():
            return out
        for key, (root, exch) in _CURVE_ROOTS.items():
            try:
                c = be.futures_curve(root, exch)
                if c.get("ratio") is not None:
                    out[key] = c["ratio"]
            except Exception:
                pass
    except Exception:
        pass
    return out


def fetch_curves(force: bool = False) -> dict:
    """The three curve ratios — FREE sources first (EIA + Cboe VIX/VIX3M), IB Gateway as fallback for
    anything missing. Cached once per run. {} if nothing reachable (engine falls back to its inputs)."""
    global _CURVES, _CURVES_SRC
    if _CURVES is not None and not force:
        return _CURVES
    c, src = {}, {}
    free = {"ng1_ng2": _eia_ratio("NG.RNGC1.D", "NG.RNGC2.D"),
            "cl1_cl2": _eia_ratio("PET.RCLC1.D", "PET.RCLC2.D"),
            "vx1_vx2": _vix_term()}
    for k, v in free.items():
        if v is not None:
            c[k], src[k] = v, "free"
    for k in [x for x in CURVE_KEY_BY_CLASS.values() if x not in c]:   # IB fallback for the gaps
        ibv = fetch_ib_curves().get(k)
        if ibv is not None:
            c[k], src[k] = ibv, "ib"
    _CURVES, _CURVES_SRC = c, src
    return c


def _apply_live_curves(ticker: str, metrics: dict) -> dict:
    """Override a vol/natgas/crude ticker's curve ratio with the live read when one is available."""
    key = CURVE_KEY_BY_CLASS.get(instrument_class(ticker))
    if key:
        live = fetch_curves().get(key)
        if live is not None:
            return {**metrics, key: live, "_curve_source": _CURVES_SRC.get(key, "live")}
    return metrics


def compute_signal(ticker: str, metrics: dict) -> dict:
    """Full decay signal for one instrument. `metrics` holds the raw inputs the class needs.
    If IB is enabled, the class's curve ratio (VX1/VX2, NG1/NG2, CL1/CL2) is overridden with the live
    read; otherwise the passed-in value is used."""
    metrics = _apply_live_curves(ticker, metrics)
    cls, scored, stand_aside, reasons = _score_all(ticker, metrics)
    base = sum(sc * wt for sc, wt in scored.values())
    conv_z = metrics.get("convexity_z", 0.0)
    mult = min(1.5, 1.0 + 0.5 * max(0.0, conv_z - 1.5))     # Level-3 multiplier (appendix C.2)
    composite = 0.0 if stand_aside else round(min(100.0, base * mult), 1)

    bucket, risk_pct = "skip", 0.0
    for thr, name, rp in SIZE:
        if composite >= thr:
            bucket, risk_pct = name, rp
            break
    hint = _structure_hint(cls, composite, metrics)
    return {
        "ticker": ticker.upper(), "instrument_class": cls, "stance": "short",
        "level12_base": round(base, 1), "level3_multiplier": round(mult, 3), "composite": composite,
        "size_bucket": bucket, "target_risk_pct": risk_pct, "conviction": round(composite / 100.0, 3),
        "stand_aside": stand_aside, "stand_aside_reason": "; ".join(reasons) or None,
        "structure_hint": hint,
        "components": {k: {"score": round(sc, 1), "weight": wt} for k, (sc, wt) in scored.items()},
        "curve_source": metrics.get("_curve_source", "input"),
        "as_of": _now_iso(),
    }


# =============================================================================
# Chain-derived signals (book appendix C.5) — Smart PCR & call convexity.
# Consumes the normalized chain from datafeed.py (legs with delta, iv, volume, mid, dte, right).
# =============================================================================
def _iv_at_delta(legs, target_delta, dte, right):
    side = [l for l in legs if l["right"] == right and l.get("iv") and l.get("delta") is not None]
    if not side:
        return None
    exp = min({l["dte"] for l in side}, key=lambda d: abs(d - dte))
    pts = sorted(((abs(l["delta"]), l["iv"]) for l in side if l["dte"] == exp))
    if not pts:
        return None
    ds = [p[0] for p in pts]; ivs = [p[1] for p in pts]
    # linear interpolation across |delta| (book C.5 np.interp)
    if target_delta <= ds[0]:
        return ivs[0]
    if target_delta >= ds[-1]:
        return ivs[-1]
    for i in range(1, len(ds)):
        if ds[i] >= target_delta:
            f = (target_delta - ds[i - 1]) / (ds[i] - ds[i - 1] or 1e-9)
            return ivs[i - 1] + f * (ivs[i] - ivs[i - 1])
    return ivs[-1]


def call_convexity(chain: dict, dte=30):
    """5-delta call IV − 25-delta call IV at ~dte (book 7.3 / C.5). Positive = euphoria/short signal."""
    legs = chain.get("legs", [])
    iv5 = _iv_at_delta(legs, 0.05, dte, "call")
    iv25 = _iv_at_delta(legs, 0.25, dte, "call")
    if iv5 is None or iv25 is None:
        return None
    return round(iv5 - iv25, 4)


def smart_pcr(chain: dict, dte_range=(30, 90), delta_range=(0.05, 0.40)):
    """Premium-weighted, OTM-only, 30–90 DTE put/call ratio (book 7.1 / C.5)."""
    put_dollar = call_dollar = 0.0
    for l in chain.get("legs", []):
        d = abs(l.get("delta") or 0); dte = l.get("dte")
        if dte is None or not (dte_range[0] <= dte <= dte_range[1]):
            continue
        if not (delta_range[0] <= d <= delta_range[1]):
            continue
        dollars = (l.get("mid") or l.get("last") or 0) * (l.get("volume") or 0)   # premium-weighted
        if l["right"] == "put":
            put_dollar += dollars
        else:
            call_dollar += dollars
    return round(put_dollar / call_dollar, 3) if call_dollar > 0 else None


def zscore(value, history):
    """Z-score of `value` against a 90-day history list (book: rolling 90-day)."""
    hist = list(history)[-90:]
    if len(hist) < 10:
        return None
    mean = sum(hist) / len(hist)
    var = sum((x - mean) ** 2 for x in hist) / (len(hist) - 1)
    sd = var ** 0.5
    return round((value - mean) / sd, 3) if sd > 1e-9 else 0.0


# =============================================================================
# Sleeve integration + daily dashboard
# =============================================================================
def to_sleeve_proposals(signals, sleeve_per_name_pct=0.5):
    """Turn qualifying signals into CIO sleeve proposals (cio.py --sleeve levered_etf ingests these)."""
    out = []
    for s in signals:
        if s["stand_aside"] or s["size_bucket"] == "skip":
            continue
        size = {"full": sleeve_per_name_pct, "half": sleeve_per_name_pct * 0.5,
                "quarter": sleeve_per_name_pct * 0.25}[s["size_bucket"]]
        cluster = {"vol": "vol", "natgas": "natgas", "crude": "crude", "metals": "metals"}.get(s["instrument_class"], "lev-equity")
        out.append({"ticker": s["ticker"], "side": "short", "size_pct_nav": round(size, 3),
                    "conviction": s["conviction"], "sector": s["instrument_class"], "cluster": cluster,
                    "max_loss_pct_nav": round(size, 3),   # defined-risk debit ≈ the size at risk
                    "structure": s["structure_hint"]["structure"]})
    return out


def daily_dashboard(signals):
    rows = sorted(signals, key=lambda s: -s["composite"])
    print(f"{'DECAY DASHBOARD':<22}{_now_iso()}")
    print(f"{'ticker':<7}{'class':<9}{'base':>6}{'x':>6}{'comp':>7}  {'action':<10}{'structure'}")
    for s in rows:
        act = "STAND-ASIDE" if s["stand_aside"] else s["size_bucket"].upper()
        print(f"{s['ticker']:<7}{s['instrument_class']:<9}{s['level12_base']:>6}{s['level3_multiplier']:>6}"
              f"{s['composite']:>7}  {act:<10}{s['structure_hint']['structure']}")
    return rows


if __name__ == "__main__":
    # demo: one instrument per class with plausible inputs
    demo = {
        "BOIL": {"ng1_ng2": 0.92, "storage_surplus_pct": 7.0, "weather_fav": 0.85, "month": 6,
                 "rv_ratio": 1.05, "iv_pct": 85.0},
        "UVXY": {"vx1_vx2": 0.91, "vix_z": 1.8, "vix_rsi": 72.0, "month": 7, "rv_iv": 1.15},
        "NVDL": {"rv_iv": 1.35, "dist_above_200sma_pct": 68.0, "weekly_rsi": 78.0, "bb_pos": 0.95,
                 "days_since_earnings": 22, "pcr_z": -1.7, "convexity_z": 2.3},
        "TSLL": {"rv_iv": 1.05, "dist_above_200sma_pct": 20.0, "weekly_rsi": 58.0, "bb_pos": 0.5,
                 "days_since_earnings": -3, "pcr_z": 0.2, "convexity_z": 0.5},
        "UCO":  {"cl1_cl2": 0.94, "inventory_surplus_pct": 6.0, "month": 6, "rv_ratio": 1.05, "iv_pct": 55.0},   # crude (contango)
        "NUGT": {"rv_iv": 1.3, "underlying_z": 2.1, "macro_tilt": 0.7, "rv_ratio": 1.1, "iv_pct": 95.0},          # gold miners 2x
        "AGQ":  {"rv_iv": 1.15, "underlying_z": 1.4, "macro_tilt": 0.6, "rv_ratio": 1.2, "iv_pct": 70.0},         # silver 2x
        "TQQQ": {"rv_iv": 1.2, "dist_above_200sma_pct": 30.0, "weekly_rsi": 72.0, "bb_pos": 0.9,
                 "pcr_z": -1.4, "convexity_z": 1.9, "iv_pct": 45.0},                                              # levered index
    }
    live = fetch_curves()
    if live:
        print(f"[decay] LIVE curves {live}  sources={_CURVES_SRC}  (override the demo inputs)\n")
    else:
        print("[decay] no live curve source reachable — using demo inputs. Set EIA_API_KEY (free, "
              "NG/CL) and/or IB_ENABLED=1 (fallback) for live curves.\n")
    sigs = [compute_signal(t, m) for t, m in demo.items()]
    daily_dashboard(sigs)
    print("\nSleeve proposals -> cio.py --sleeve levered_etf:")
    print(json.dumps(to_sleeve_proposals(sigs), indent=2))
