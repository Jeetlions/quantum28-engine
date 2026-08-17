#!/usr/bin/env python3
"""
gex_algo.py — dealer Gamma Exposure (GEX), walls, flip, and pin.

This is the algorithm the framework uses to place 0DTE butterfly legs, so it follows the
book's documented methodology exactly ("Gamma Exposure & Dealer Positioning", Part III + the
Methodology appendix). datafeed.py auto-detects this file and uses compute_gex() in place of the
placeholder.

Methodology (book, verbatim intent):
  per-contract GEX  =  gamma × open_interest × 100 × spot² × 0.01     (dollar hedge / 1% move)
  sign convention   =  CALLS positive, PUTS negative (dealers long calls, short puts)
  Net GEX           =  signed sum across all strikes & expirations
  per-strike profile=  signed sum at each strike (near-dated dominate via their larger gamma)
  call wall         =  strike ABOVE spot with the largest POSITIVE per-strike GEX
  put wall          =  strike BELOW spot with the largest-MAGNITUDE NEGATIVE per-strike GEX
  gamma flip        =  walk the per-strike profile outward; where CUMULATIVE signed GEX crosses 0
  strike grid       =  ignore strikes beyond ±50% of spot (negligible contribution)
  noise gates       =  OI (thin-strike), structural (min share of profile), actionable-distance
  constants (appendix): pin saturation 0.3% of spot; long-gamma saturation $1B net GEX

Everything below the CONSTANTS block is grounded in that text. The constants are the book's
"reasonable starting points, not fixed laws" — adjust per underlying.

Contract:  compute_gex(chain, contract_multiplier=100, dte_max=None) -> gamma block dict:
  {net_gex_bn, gamma_regime, gamma_flip, call_wall, put_wall, gamma_pin, computed_at, ...}
where chain = {"symbol","spot","legs":[{strike,right,gamma?,iv?,dte?,open_interest}, ...]}.
If a leg lacks gamma, it is computed from a Black–Scholes model using the leg's IV (book Step 2).

Pure standard library.
"""
from __future__ import annotations
import math
from datetime import datetime, timezone

# ---- constants (book appendix — adjustable starting points) ------------------
CALL_SIGN, PUT_SIGN = 1.0, -1.0
GRID_MAX_PCT = 0.50            # ignore strikes beyond ±50% of spot (Step 1)
OI_MIN_FRAC = 0.05            # interior/OI gate: strike OI >= this × the busiest strike's OI
STRUCT_MIN_FRAC = 0.03       # structural gate: |contribution| >= this × total |profile|
ACTIONABLE_MIN_PCT = 0.0     # actionable-distance gate: drop walls closer than this × spot (0 = off; raise for 0DTE cleanliness)
PIN_WINDOW_PCT = 0.02        # search the pin within ±2% of spot (the near-money magnet)
PIN_SATURATION_PCT = 0.003   # 0.3% of spot — book pin-saturation constant
LONG_GAMMA_SATURATION_BN = 1.0  # $1B net GEX — regime-strength saturation (book)
RISK_FREE = 0.045            # r for the BS gamma fallback only


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _bs_gamma(spot, strike, iv, dte_years, r=RISK_FREE):
    """Black–Scholes gamma — used only when a leg has no model gamma but has IV (book Step 2)."""
    if not (spot > 0 and strike > 0 and iv and iv > 0 and dte_years and dte_years > 0):
        return 0.0
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * dte_years) / (iv * math.sqrt(dte_years))
    return _norm_pdf(d1) / (spot * iv * math.sqrt(dte_years))


def _leg_gamma(leg, spot):
    g = leg.get("gamma")
    if g is not None:
        return float(g)
    dte = leg.get("dte")
    T = (float(dte) / 365.0) if dte else None
    return _bs_gamma(spot, float(leg["strike"]), leg.get("iv"), T)


def compute_gex(chain: dict, contract_multiplier: int = 100, dte_max: int | None = None, **kw) -> dict:
    """Compute the gamma block for options_view.json from a normalized chain, per the book."""
    spot = float(chain.get("spot") or 0)
    if spot <= 0:
        raise ValueError("compute_gex: chain needs a positive 'spot'")
    lo, hi = spot * (1 - GRID_MAX_PCT), spot * (1 + GRID_MAX_PCT)

    signed, absgex, oi_by = {}, {}, {}          # per-strike: signed GEX, |GEX|, total OI
    for leg in chain.get("legs", []):
        k = float(leg["strike"])
        if k < lo or k > hi:                     # Step 1: strike grid (±50% of spot)
            continue
        if dte_max is not None and (leg.get("dte") is None or leg["dte"] > dte_max):
            continue                              # optionally restrict to near-dated (e.g. 0DTE)
        oi = float(leg.get("open_interest") or 0)
        if oi <= 0:
            continue
        gamma = _leg_gamma(leg, spot)
        if gamma <= 0:
            continue
        gex = gamma * oi * contract_multiplier * spot * spot * 0.01   # Part III / Step 2 formula
        sign = CALL_SIGN if leg["right"] == "call" else PUT_SIGN
        signed[k] = signed.get(k, 0.0) + sign * gex
        absgex[k] = absgex.get(k, 0.0) + gex
        oi_by[k] = oi_by.get(k, 0.0) + oi

    if not signed:
        return {"net_gex_bn": 0.0, "gamma_regime": "positive", "gamma_flip": None,
                "call_wall": None, "put_wall": None, "gamma_pin": None,
                "computed_at": _now_iso(), "_source": "gex_algo (no usable legs)"}

    # Step 3: aggregate to Net GEX (and Total GEX)
    net = sum(signed.values())
    total = sum(absgex.values())
    net_bn = net / 1e9
    regime = "positive" if net >= 0 else "negative"
    strength = round(math.tanh(net_bn / LONG_GAMMA_SATURATION_BN), 3)   # bounded regime score

    # Step 4 + gates: walls
    max_oi = max(oi_by.values())
    total_abs = sum(abs(v) for v in signed.values()) or 1.0
    def _passes(k):
        if oi_by[k] < OI_MIN_FRAC * max_oi:                      # OI / thin-strike gate
            return False
        if abs(signed[k]) < STRUCT_MIN_FRAC * total_abs:          # structural gate
            return False
        if abs(k - spot) < ACTIONABLE_MIN_PCT * spot:            # actionable-distance gate
            return False
        return True
    above = [k for k in signed if k > spot and signed[k] > 0 and _passes(k)]
    below = [k for k in signed if k < spot and signed[k] < 0 and _passes(k)]
    call_wall = max(above, key=lambda k: signed[k]) if above else None
    put_wall = min(below, key=lambda k: signed[k]) if below else None

    # Step 4: gamma flip — the price level where the CUMULATIVE signed profile crosses zero.
    # The book says "walk outward from spot": we collect every zero-crossing of the cumulative
    # per-strike profile and take the one NEAREST spot (the regime boundary you actually trade).
    # (Per-strike on the feed's provided gamma is the right method for 0DTE, where a Black–Scholes
    #  reprice degenerates as T -> 0; for multi-day names a spot-shift reprice is an alternative.)
    crossings = []
    cum = 0.0; prev_k = None; prev_cum = 0.0
    for k in sorted(signed):
        cum += signed[k]
        if prev_k is not None and ((prev_cum < 0 <= cum) or (prev_cum > 0 >= cum)) and cum != prev_cum:
            crossings.append(round(prev_k + (k - prev_k) * (0.0 - prev_cum) / (cum - prev_cum), 2))
        prev_k, prev_cum = k, cum
    flip = min(crossings, key=lambda x: abs(x - spot)) if crossings else None

    # Pin: near-money gamma magnet, proximity-weighted with the 0.3%-of-spot saturation
    sat = PIN_SATURATION_PCT * spot
    win = PIN_WINDOW_PCT * spot
    pin, pin_score = None, -1.0
    for k in absgex:
        dist = abs(k - spot)
        if dist > win:
            continue
        w = 1.0 if dist <= sat else sat / dist
        score = absgex[k] * w
        if score > pin_score:
            pin, pin_score = k, score

    return {
        "net_gex_bn": round(net_bn, 3),
        "gamma_regime": regime,
        "regime_strength": strength,                 # −1..1, saturates at ±$1B
        "gamma_flip": flip,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "gamma_pin": pin,
        "total_gex_bn": round(total / 1e9, 3),
        "n_strikes": len(signed),
        "computed_at": _now_iso(),
        "_source": "gex_algo (book methodology)",
    }


if __name__ == "__main__":
    # quick self-check on a small asymmetric chain
    import json
    spot = 5960.0
    legs = []
    for k in range(5900, 6021, 20):
        gamma = math.exp(-((k - spot) ** 2) / (2 * (0.02 * spot) ** 2)) / (spot * 0.14 * math.sqrt(1 / 365)) * 0.01
        call_oi = 4000 if k in (5980, 6000) else 800        # heavy calls above -> call wall
        put_oi = 4500 if k in (5920, 5900) else 700          # heavy puts below -> put wall
        legs.append({"strike": k, "right": "call", "gamma": gamma, "open_interest": call_oi, "dte": 0})
        legs.append({"strike": k, "right": "put", "gamma": gamma, "open_interest": put_oi, "dte": 0})
    print(json.dumps(compute_gex({"symbol": "SPX", "spot": spot, "legs": legs}), indent=2))
