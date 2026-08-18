#!/usr/bin/env python3
"""
Options Strategist — a CONSTRUCTOR, not a voter.

Given the fleet's views and the Risk Manager's approved direction, it selects the right
multi-leg structure (vertical / diagonal / calendar / double calendar / butterfly /
broken-wing / iron condor) and lays out the legs, greeks, breakevens, max loss/gain, and
a probability-of-profit estimate. It writes shared/structure_ticket.json.

It knows equities from indices: SPX/NDX/RUT/XSP are cash-settled, European-style (no early
assignment, no pin-into-shares), which changes assignment/exercise handling.

NOTE: metrics here are transparent heuristics for the example — a production strategist
would call a real pricer/greeks engine and an IV surface. The SELECTION logic is the
teachable part; the numbers are illustrative.
"""
from __future__ import annotations
import os
from datetime import datetime, timezone, date, timedelta

INDEX_TICKERS = {"SPX", "SPXW", "NDX", "NDXP", "RUT", "XSP", "VIX"}
INC = 5                       # strike increment used for rounding in the example
WIDTH = 10                    # default spread width (equities)
IVR_HIGH = 50                 # iv_rank >= this => prefer premium selling
NEUTRAL_BAND = 0.20           # |vote_net| below this reads as non-directional
FRESH_MIN_0DTE = 30           # a 0DTE gamma read older than this (min) is not trusted — walls rebuild same-day
FLY_EARLIEST_ET = 11.5        # don't establish a 0DTE PINNING fly before this ET hour — the wall is still
                              # forming on the morning's flow; wait for the stabilized midday read


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_calib():
    """Tuned entry parameters from calibration.py (e.g. vertical IV-rank pivot). Empty on miss."""
    import json
    from pathlib import Path
    f = Path(__file__).resolve().parent / "benchmarks" / "calibration.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            return {}
    return {}


def _rs(x):                   # round to a listed strike
    return int(round(x / INC) * INC)


def _exp(days):
    return (date.today() + timedelta(days=max(1, int(days)))).isoformat()


def _leg(action, right, strike, expiry, qty=1):
    return {"action": action, "right": right, "strike": strike, "expiry": expiry, "qty": qty}


def build_structure(ticker, side, vote_net, views):
    f = views.get("fundamental-analyst", {})
    t = views.get("technical-analyst", {})
    o = views.get("options-analyst", {}).get("options", {})
    c = views.get("catalyst-analyst", {}).get("catalyst", {})

    _g0 = o.get("gamma", {}) or {}
    spot = (o.get("spot") or _g0.get("spot")                  # live underlying from the feed (0DTE/index)
            or (f.get("fair_value", {}) or {}).get("current")  # equity vote: from the fundamental view
            or _g0.get("gamma_pin") or _g0.get("gamma_flip") or _g0.get("call_wall")  # real gamma level
            or 100.0)
    lvl = t.get("levels", {}) or {}
    support = lvl.get("support", _rs(spot * 0.95))
    resistance = lvl.get("resistance", _rs(spot * 1.06))
    fv = f.get("fair_value", {}) or {}
    fv_base = fv.get("base", _rs(spot * 1.1))
    floor = f.get("downside_floor", support)

    iv_rank = float(o.get("iv_rank", 40))
    _calib = _load_calib()                                    # tuned entry knobs from calibration.py
    v_pivot = float(_calib.get("vertical", {}).get("iv_rank_pivot", IVR_HIGH))   # credit/debit split
    cbf = _calib.get("butterfly", {}); cdg = _calib.get("diagonal", {}); ccl = _calib.get("calendar", {})
    term = o.get("term_structure", "flat")
    em_pct = float(c.get("expected_move_pct", 0) or 0) / 100.0
    em = max(spot * em_pct, INC)                     # expected move in $, at least one strike
    cat_date = c.get("date")

    # days to the event
    dte_event = None
    if cat_date:
        try:
            dte_event = (date.fromisoformat(cat_date) - date.today()).days
        except Exception:
            dte_event = None
    event_near = dte_event is not None and 0 < dte_event <= 45

    # direction
    if side == "long":
        bias = "bullish"
    elif side == "short":
        bias = "bearish"
    else:
        bias = "neutral"
    if abs(float(vote_net or 0)) < NEUTRAL_BAND:
        bias = "neutral"

    is_index = ticker.upper() in INDEX_TICKERS
    front_days = (dte_event + 2) if event_near else 35
    front = _exp(front_days)
    back = _exp(front_days + 35)                                          # generic default gap
    back_diag = _exp(front_days + int(cdg.get("tenor_gap_days", 35)))     # calibrated diagonal gap
    back_cal = _exp(front_days + int(ccl.get("tenor_gap_days", 35)))      # calibrated calendar gap

    # ---------------- strategy selection (the teachable core) -----------------
    g = o.get("gamma", {}) or {}
    pin_ok = g.get("gamma_regime") == "positive" and (g.get("gamma_pin") or g.get("call_wall"))

    # 0DTE INDEX (SPX/NDX): same-day expiry is a pure gamma trade. In POSITIVE gamma the pin is a
    # strong intraday magnet -> zero-cost broken-wing on the pin. In NEGATIVE gamma the index is a
    # BREAKOUT candidate (dealers amplify the move) -> a pinning fly is the WRONG trade; go long
    # gamma / directional instead. And because same-day walls rebuild intraday, the gamma read must
    # be FRESH — a stale read means no trade.
    tenor = o.get("tenor")
    is_zero_dte = is_index and tenor == "0DTE"
    gamma_fresh, stale_min = _gamma_freshness(g, is_zero_dte)
    exp0 = date.today().isoformat()

    if is_zero_dte and not gamma_fresh:
        strat, legs, metrics, why = _stand_aside_0dte(stale_min)
    elif is_zero_dte and g.get("gamma_regime") == "positive" and (g.get("gamma_pin") or g.get("call_wall")):
        et = _session_et_hour()
        if et < FLY_EARLIEST_ET:
            # a PINNING fly is short gamma: don't put it on into the noisy morning, and the 9:30 wall
            # is built on still-forming flow. Wait for the stabilized midday read.
            strat, legs, metrics, why = _stand_aside_0dte(
                stale_min, reason=f"0DTE pinning fly: it's {_et_str(et)} — the same-day wall is still forming; "
                                  f"wait for the midday read (>= {_et_str(FLY_EARLIEST_ET)}) before centering a fly")
        else:
            strat, legs, metrics, why = _butterfly(spot, em, exp0, gamma=g, zero_cost=True,
                                                   width_mult=float(cbf.get("wing_width_em_mult", 1.0)))
            why = why + [f"0DTE {ticker}: established at {_et_str(et)} on the stabilized midday pin; "
                         f"positive-gamma magnet tightens into the close (read {stale_min}m old)."]
    elif is_zero_dte and g.get("gamma_regime") == "negative":
        flip = g.get("gamma_flip")
        bias = "bearish" if (flip and spot < flip) else ("bullish" if (flip and spot > flip) else "neutral")
        strat, legs, metrics, why = _breakout_0dte(spot, g, exp0, ticker)
    elif is_zero_dte:
        strat, legs, metrics, why = _stand_aside_0dte(None, reason="no clear gamma regime / pin — no 0DTE edge")
    elif bias == "neutral" and pin_ok:
        # positive-gamma pinning regime -> a butterfly centered on the gamma pin
        strat, legs, metrics, why = _butterfly(spot, em, front, gamma=g, zero_cost=True,
                                               width_mult=float(cbf.get("wing_width_em_mult", 1.0)))
    elif bias == "neutral" and iv_rank >= IVR_HIGH:
        strat, legs, metrics, why = _iron_condor(spot, support, resistance, em, front)
    elif bias == "neutral" and event_near and term == "backwardation":
        if em > 1.2 * INC:
            strat, legs, metrics, why = _double_calendar(support, resistance, front, back_cal)
        else:
            strat, legs, metrics, why = _calendar(_rs(spot), front, back_cal)
    elif bias in ("bullish", "bearish") and event_near and term == "backwardation" and em <= 1.2 * INC:
        # DIRECTIONAL (OTM) calendar: place the strike OTM toward the drift (calibrated offset)
        off = float(ccl.get("strike_offset_pct", 0.0))
        k = _rs(spot * (1 + off)) if bias == "bullish" else _rs(spot * (1 - off))
        strat, legs, metrics, why = _calendar(k, front, back_cal, directional=bias, offset=off)
    elif bias in ("bullish", "bearish") and event_near and term == "backwardation":
        strat, legs, metrics, why = _diagonal(bias, spot, resistance, support, front, back_diag,
                                              dist=float(cdg.get("short_strike_dist_pct", 0.0)))
    elif bias in ("bullish", "bearish") and iv_rank < 30 and em > 1.2 * INC:
        # cheap vol + a big expected move -> LONG-gamma breakout via a ratio BACKSPREAD (~zero cost)
        strat, legs, metrics, why = _ratio_backspread(bias, spot, resistance if bias == "bullish" else support, front)
    elif bias in ("bullish", "bearish") and iv_rank >= 65:
        # very rich vol + a target -> defined front RATIO (sell 2, cap the tail with a wing) for income
        strat, legs, metrics, why = _ratio_frontspread(bias, spot, resistance if bias == "bullish" else support, front)
    elif bias in ("bullish", "bearish") and iv_rank >= v_pivot:
        strat, legs, metrics, why = _vertical_credit(bias, floor, support, resistance, front)
    elif bias in ("bullish", "bearish"):
        strat, legs, metrics, why = _vertical_debit(bias, spot, resistance, support, front)
    else:
        # directional / OTM fly toward the target, with a calibrated center offset + wing width
        off = float(cbf.get("center_offset_pct", 0.0)) if bias in ("bullish", "bearish") else 0.0
        base_center = fv_base if bias != "bearish" else support
        center = base_center * (1 + off) if bias == "bullish" else (base_center * (1 - off) if bias == "bearish" else base_center)
        strat, legs, metrics, why = _butterfly(center, em, front, gamma=g,
                                               width_mult=float(cbf.get("wing_width_em_mult", 1.0)))

    ticket = {
        "schema_version": "1.0", "agent": "options-strategist", "ticker": ticker,
        "as_of": _now_iso(),
        "underlying_type": "index" if is_index else "equity",
        "settlement": "cash" if is_index else "physical",
        "exercise_style": "european" if is_index else "american",
        "direction_bias": bias,
        "vol_regime": {"iv_rank": iv_rank, "term_structure": term},
        "strategy": strat, "legs": legs,
        "rationale": why,
        "notes": [
            ("Index: cash-settled, European — no early assignment or pin-into-shares."
             if is_index else
             "Equity: American — watch early assignment on short legs (dividends/deep ITM) and pin risk at expiry."),
            "Metrics are illustrative heuristics; price against a live surface before trading.",
        ],
    }
    ticket.update(metrics)
    if is_zero_dte:
        ticket["tenor"] = "0DTE"
        ticket["gamma_read"] = {
            "computed_at": g.get("computed_at"), "fresh": gamma_fresh, "minutes_old": stale_min,
            "regime": g.get("gamma_regime"), "flip": g.get("gamma_flip"),
            "call_wall": g.get("call_wall"), "put_wall": g.get("put_wall"), "pin": g.get("gamma_pin"),
            "wall_reliability": ("high intraday pin, but recompute — same-day walls migrate"
                                 if gamma_fresh else "stale — wall no longer trustworthy for 0DTE"),
        }
    return ticket


# ---------------- 0DTE index helpers -----------------------------------------
def _session_et_hour():
    """Current US/Eastern hour as a decimal (e.g. 12.5 = 12:30). Override with env Q28_SESSION_ET
    for backtests/tests. Falls back to a fixed EDT offset if zoneinfo is unavailable."""
    ov = os.environ.get("Q28_SESSION_ET")
    if ov is not None:
        try:
            return float(ov)
        except ValueError:
            pass
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now = datetime.now(timezone.utc) - timedelta(hours=4)   # EDT fallback
    return now.hour + now.minute / 60.0


def _et_str(h):
    return f"{int(h):02d}:{int(round((h - int(h)) * 60)):02d} ET"


def _gamma_freshness(g, is_zero_dte):
    """Fresh enough to trust the wall/pin? For 0DTE a read must be recent (walls rebuild same-day);
    for longer tenors a timestamp is nice-to-have, not required. Returns (fresh?, minutes_old)."""
    ca = g.get("computed_at")
    if not ca:
        return (not is_zero_dte, None)          # no timestamp: ok for weeklies+, NOT trusted for 0DTE
    try:
        t = datetime.fromisoformat(ca.replace("Z", "+00:00"))
        mins = round((datetime.now(timezone.utc) - t).total_seconds() / 60.0)
    except Exception:
        return (not is_zero_dte, None)
    if not is_zero_dte:
        return (True, mins)
    return (mins <= FRESH_MIN_0DTE, mins)


def _stand_aside_0dte(stale_min, reason=None):
    if reason is None:
        age = f"stale ({stale_min}m old > {FRESH_MIN_0DTE}m)" if stale_min is not None else "missing its computed_at timestamp"
        reason = (f"gamma read is {age} — the 0DTE wall/pin rebuilds on same-day flow and is "
                  f"no longer reliable; refresh before trading")
    m = {"net": {"type": "none", "amount": 0.0}, "max_loss": 0.0, "max_gain": 0.0,
         "breakevens": [], "pop_pct": 0, "net_greeks": {}}
    return "no_trade", [], m, ["0DTE stand aside — no trade", reason]


def _breakout_0dte(spot, g, exp, ticker):
    """Negative-gamma 0DTE: the index is a breakout candidate. Flip the expression to LONG gamma —
    a ratio BACKSPREAD toward the broken wall when the flip gives direction, or a REVERSE (short)
    fly when it doesn't. Never a pinning fly (that is short the gamma that's accelerating)."""
    flip = g.get("gamma_flip"); cw = g.get("call_wall"); pw = g.get("put_wall")
    if flip and spot < flip:
        strat, legs, m, why = _ratio_backspread("bearish", spot, pw or spot * 0.98, exp)
        return strat, legs, m, [f"0DTE {ticker}: NEGATIVE-gamma (breakout) — spot {spot} below flip {flip}; "
                                f"dealers amplify the drop."] + why
    if flip and spot > flip:
        strat, legs, m, why = _ratio_backspread("bullish", spot, cw or spot * 1.02, exp)
        return strat, legs, m, [f"0DTE {ticker}: NEGATIVE-gamma (breakout) — spot {spot} above flip {flip}; "
                                f"dealers amplify the rise."] + why
    strat, legs, m, why = _reverse_butterfly(spot, max(spot * 0.01, INC), exp)
    return strat, legs, m, [f"0DTE {ticker}: NEGATIVE-gamma (breakout), direction unclear (no flip) — "
                            f"reverse fly for a move either way."] + why


def _ratio_backspread(bias, spot, target, exp):
    """Ratio BACKSPREAD (1x2): sell 1 near, buy 2 toward the break. NET LONG options -> LONG GAMMA,
    ~zero cost, small defined loss if it stalls, large gain on a fast directional break."""
    if bias == "bearish":
        k1 = _rs(spot); k2 = _rs(min(target, spot * 0.97)); right = "put"
        be = round(k2 - abs(k1 - k2), 2)
        greeks = {"delta": "-", "theta": "- (net long)", "vega": "+", "gamma": "+ (long — breakout)"}
        why = [f"{bias} breakout via PUT ratio backspread: sell 1 {k1}, buy 2 {k2} — long gamma, ~zero cost.",
               "small, defined loss if it stalls between strikes; large gain on a fast break down."]
    else:
        k1 = _rs(spot); k2 = _rs(max(target, spot * 1.03)); right = "call"
        be = round(k2 + abs(k2 - k1), 2)
        greeks = {"delta": "+", "theta": "- (net long)", "vega": "+", "gamma": "+ (long — breakout)"}
        why = [f"{bias} breakout via CALL ratio backspread: sell 1 {k1}, buy 2 {k2} — long gamma, ~zero cost.",
               "small, defined loss if it stalls between strikes; large gain on a fast break up."]
    legs = [_leg("sell", right, k1, exp, 1), _leg("buy", right, k2, exp, 2)]
    width = abs(k2 - k1) or WIDTH
    m = {"net": {"type": "credit", "amount": 0.0}, "max_loss": round(width, 2),
         "max_gain": "large / effectively uncapped on a fast directional break",
         "breakevens": [be], "pop_pct": 42, "net_greeks": greeks}
    return "ratio_backspread", legs, m, why


def _ratio_frontspread(bias, spot, target, exp):
    """Defined FRONT RATIO (1x2 + protective wing): buy 1, sell 2 at the target, buy 1 wing to cap
    the tail. NET SHORT options -> short gamma income with a directional lean; best if price drifts
    to the short strikes. The wing converts the classic ratio's naked tail into DEFINED risk."""
    if bias == "bearish":
        k1 = _rs(spot); k2 = _rs(min(target, spot * 0.97)); k3 = _rs(k2 - abs(k1 - k2)); right = "put"
    else:
        k1 = _rs(spot); k2 = _rs(max(target, spot * 1.03)); k3 = _rs(k2 + abs(k2 - k1)); right = "call"
    legs = [_leg("buy", right, k1, exp, 1), _leg("sell", right, k2, exp, 2), _leg("buy", right, k3, exp, 1)]
    width = abs(k2 - k1) or WIDTH; credit = round(0.20 * width, 2)
    m = {"net": {"type": "credit", "amount": credit}, "max_loss": round(width - credit, 2),
         "max_gain": round(width + credit, 2), "breakevens": [k2, round(k2 + (width if bias != "bearish" else -width), 2)],
         "pop_pct": 55, "net_greeks": {"delta": ("+" if bias != "bearish" else "-"), "theta": "+", "vega": "-", "gamma": "- (short)"}}
    return "ratio_frontspread", legs, m, [
        f"{bias} income via DEFINED front ratio: buy 1 {k1}, sell 2 {k2}, buy wing {k3} (caps the tail).",
        f"collects premium; best if price drifts to {k2}; short gamma/vega, defined risk."]


def _reverse_butterfly(center, em, exp):
    """Reverse (short) butterfly: sell 1 wing / buy 2 body / sell 1 wing -> NET CREDIT, LONG gamma.
    A defined-risk 'I expect a move but not which way' breakout: max loss at the center, keeps the
    credit if it expires beyond a wing. Cheaper and defined vs. buying a straddle."""
    c = _rs(center); w = max(_rs(em), INC)
    legs = [_leg("sell", "call", c - w, exp, 1), _leg("buy", "call", c, exp, 2), _leg("sell", "call", c + w, exp, 1)]
    credit = round(0.55 * w, 2)
    m = {"net": {"type": "credit", "amount": credit}, "max_loss": round(w - credit, 2),
         "max_gain": credit, "breakevens": [round(c - w + credit, 2), round(c + w - credit, 2)], "pop_pct": 46,
         "net_greeks": {"delta": "~0 near center", "theta": "- (long body)", "vega": "+", "gamma": "+ (long — wants a move)"}}
    return "reverse_butterfly", legs, m, [
        f"reverse (short) fly centered {c}, wings ±{w}: sell wings / buy 2 body — long gamma, defined risk.",
        "profits on a move to either wing; max loss if it pins the center; cheaper/defined vs. a straddle."]


# ---------------- constructors (illustrative metrics) -------------------------
def _iron_condor(spot, support, resistance, em, exp):
    sp = _rs(min(support, spot - em)); lp = sp - WIDTH
    sc = _rs(max(resistance, spot + em)); lc = sc + WIDTH
    credit = round(0.30 * WIDTH, 2)                    # ~1/3 width for OTM condor
    legs = [_leg("buy", "put", lp, exp), _leg("sell", "put", sp, exp),
            _leg("sell", "call", sc, exp), _leg("buy", "call", lc, exp)]
    m = {"net": {"type": "credit", "amount": credit},
         "max_loss": round(WIDTH - credit, 2), "max_gain": credit,
         "breakevens": [sp - credit, sc + credit], "pop_pct": round(100 * (1 - credit / WIDTH)),
         "net_greeks": {"delta": "~0", "theta": "+", "vega": "-", "gamma": "-"}}
    return "iron_condor", legs, m, [
        "Neutral view + high IV rank → sell premium in a defined-risk condor",
        f"short strikes outside expected move / at support {support} & resistance {resistance}"]


def _calendar(k, front, back, directional=None, offset=0.0):
    debit = round(0.02 * k, 2)
    right = "call" if (directional in (None, "bullish")) else "put"   # OTM put calendar for a bearish lean
    legs = [_leg("sell", right, k, front), _leg("buy", right, k, back)]
    m = {"net": {"type": "debit", "amount": debit},
         "max_loss": debit, "max_gain": "path-dependent (~1.5-2x debit near strike at front expiry)",
         "breakevens": [k - debit, k + debit], "pop_pct": 55,
         "net_greeks": {"delta": ("~0" if not directional else ("+" if directional == "bullish" else "-")),
                        "theta": "+", "vega": "+", "gamma": "-"}}
    if directional:
        why = [f"{directional} + event + backwardation → OTM ({offset*100:.1f}%) {right} calendar: drift toward {k}",
               f"same strike {k} front ({front}) / back ({back}); short front IV crush, long back vega"]
    else:
        why = ["Neutral + event + backwardation → ATM calendar: short front IV crush, long back vega",
               f"same strike {k} in front ({front}) and back ({back}) expiries"]
    return "calendar", legs, m, why


def _double_calendar(support, resistance, front, back):
    kp, kc = _rs(support), _rs(resistance)
    debit = round(0.04 * ((kp + kc) / 2), 2)
    legs = [_leg("sell", "put", kp, front), _leg("buy", "put", kp, back),
            _leg("sell", "call", kc, front), _leg("buy", "call", kc, back)]
    m = {"net": {"type": "debit", "amount": debit},
         "max_loss": debit, "max_gain": "path-dependent; widest tent between the two strikes",
         "breakevens": ["below ~" + str(kp), "above ~" + str(kc)], "pop_pct": 58,
         "net_greeks": {"delta": "~0", "theta": "+", "vega": "+", "gamma": "-"}}
    return "double_calendar", legs, m, [
        "Neutral + event + wide expected move → double calendar widens the profit tent",
        f"put calendar at support {kp}, call calendar at resistance {kc}"]


def _diagonal(bias, spot, resistance, support, front, back, dist=0.0):
    # dist (calibrated) = how far BEYOND resistance/support to sell the front short strike
    if bias == "bullish":
        long_k = _rs(spot); short_k = _rs(resistance * (1 + dist))
        legs = [_leg("buy", "call", long_k, back), _leg("sell", "call", short_k, front)]
        greeks = {"delta": "+", "theta": "+ (short decays faster)", "vega": "+ (net long back)", "gamma": "mixed"}
    else:
        long_k = _rs(spot); short_k = _rs(support * (1 - dist))
        legs = [_leg("buy", "put", long_k, back), _leg("sell", "put", short_k, front)]
        greeks = {"delta": "-", "theta": "+ (short decays faster)", "vega": "+ (net long back)", "gamma": "mixed"}
    debit = round(abs(_rs(resistance) - _rs(spot)) * 0.45 + 1, 2)
    m = {"net": {"type": "debit", "amount": debit},
         "max_loss": f"~net debit {debit} (approx; back-month retains value)",
         "max_gain": "maximized near the short strike at front expiry",
         "breakevens": [round(spot + debit if bias == "bullish" else spot - debit, 2)],
         "pop_pct": 50, "net_greeks": greeks}
    return "diagonal", legs, m, [
        f"{bias} + event + backwardation → diagonal: buy back-month {long_k}, sell front-month {short_k}",
        "long back-month vega, financed by the richer front-month you sell"]


def _vertical_credit(bias, floor, support, resistance, exp):
    if bias == "bullish":
        sp = _rs(min(floor, support)); lp = sp - WIDTH
        legs = [_leg("sell", "put", sp, exp), _leg("buy", "put", lp, exp)]
        be = sp - round(0.33 * WIDTH, 2)
        greeks = {"delta": "+", "theta": "+", "vega": "-"}
        why = f"bullish + high IV → bull put credit spread below floor/support ({sp}/{lp})"
    else:
        sc = _rs(resistance); lc = sc + WIDTH
        legs = [_leg("sell", "call", sc, exp), _leg("buy", "call", lc, exp)]
        be = sc + round(0.33 * WIDTH, 2)
        greeks = {"delta": "-", "theta": "+", "vega": "-"}
        why = f"bearish + high IV → bear call credit spread above resistance ({sc}/{lc})"
    credit = round(0.33 * WIDTH, 2)
    m = {"net": {"type": "credit", "amount": credit}, "max_loss": round(WIDTH - credit, 2),
         "max_gain": credit, "breakevens": [round(be, 2)], "pop_pct": round(100 * (1 - credit / WIDTH)),
         "net_greeks": greeks}
    return "vertical_credit", legs, m, [why, "sell premium with defined risk in the direction of the view"]


def _vertical_debit(bias, spot, resistance, support, exp):
    if bias == "bullish":
        lk = _rs(spot); sk = _rs(resistance)
        legs = [_leg("buy", "call", lk, exp), _leg("sell", "call", sk, exp)]
        be = lk + round(0.4 * (sk - lk), 2); greeks = {"delta": "+", "theta": "-", "vega": "+"}
        why = f"bullish + low IV → bull call debit spread ({lk}/{sk})"
    else:
        lk = _rs(spot); sk = _rs(support)
        legs = [_leg("buy", "put", lk, exp), _leg("sell", "put", sk, exp)]
        be = lk - round(0.4 * (lk - sk), 2); greeks = {"delta": "-", "theta": "-", "vega": "+"}
        why = f"bearish + low IV → bear put debit spread ({lk}/{sk})"
    width = abs(_rs(resistance) - _rs(spot)) or WIDTH
    debit = round(0.4 * width, 2)
    m = {"net": {"type": "debit", "amount": debit}, "max_loss": debit,
         "max_gain": round(width - debit, 2), "breakevens": [round(be, 2)],
         "pop_pct": 50, "net_greeks": greeks}
    return "vertical_debit", legs, m, [why, "buy directional exposure with capped risk when IV is cheap"]


def _butterfly(center, em, exp, gamma=None, zero_cost=False, width_mult=1.0):
    g = gamma or {}
    # Center on the dealer GAMMA PIN (or call wall) when available — that is where price
    # gets magnetized into expiry in a positive-gamma regime; else the given target.
    pinned = g.get("gamma_pin") or g.get("call_wall")
    c = _rs(pinned if pinned else center)
    w = max(_rs(em * width_mult), INC)                    # calibrated wing width (x expected move)
    pin_note = (f" on the gamma pin {c} (net GEX {g.get('net_gex_bn')}bn, "
                f"positive/pinning)" if pinned else f" at {c}")

    if zero_cost:
        # ZERO-COST BROKEN-WING: unequal wings — the wider far wing finances the body so the
        # structure goes on for ~zero debit (or a small credit). Defined risk, concentrated
        # on the near/broken side; keeps a directional lean toward the pin.
        low, hi = c - w, c + 2 * w
        legs = [_leg("buy", "call", low, exp), _leg("sell", "call", c, exp, 2), _leg("buy", "call", hi, exp)]
        m = {"net": {"type": "credit", "amount": 0.0},
             "max_loss": f"defined ≈ lower-wing width {w} (only if it expires below {low})",
             "max_gain": f"≈ {w} near the pin {c} at expiry",
             "breakevens": [low, hi], "pop_pct": 48,
             "net_greeks": {"delta": "small (toward pin)", "theta": "+", "vega": "-"}}
        return "broken_wing_butterfly", legs, m, [
            f"zero-cost broken-wing call fly{pin_note}; wings −{w} / +{2 * w}",
            "the wider upper wing finances the body for ~zero cost; max profit at the pin, defined risk"]

    legs = [_leg("buy", "call", c - w, exp), _leg("sell", "call", c, exp, 2), _leg("buy", "call", c + w, exp)]
    debit = round(0.15 * w, 2)
    m = {"net": {"type": "debit", "amount": debit}, "max_loss": debit,
         "max_gain": round(w - debit, 2), "breakevens": [c - w + debit, c + w - debit],
         "pop_pct": 40, "net_greeks": {"delta": "~0 near center", "theta": "+", "vega": "-"}}
    return "butterfly", legs, m, [
        f"call butterfly centered{pin_note}, wings ±{w} (expected move)",
        "cheap, defined risk; the broken-wing variant finances one wing for ~zero cost"]
