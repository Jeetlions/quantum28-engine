#!/usr/bin/env python3
"""
quant_inputs.py — Tasty price-history for the deterministic/half-deterministic seats.

Technical and Quant are now REASONING bots (they read real structure — prior highs/lows, where
price reacted — instead of rigid MA/RSI). This module's job for them is to (1) hand the bot the
factual price history to reason over (`price_context`), and (2) BOUND whatever level the bot returns
to the real recent range (`sanitize_view`), so a hallucinated stop can never poison sizing/strikes.
If no bot view is supplied, the computed `technical_view` / `quant_view` serve as a fallback.

Liquidity stays computed — it's a measurement (avg daily $ volume), not a judgment.

What Tasty price/volume supports (free):
  Technical  — real swing H/L support/resistance + range for the bot; engine bounds its stop/levels.
  Quant      — momentum / low-vol from price; value/quality/size need fundamentals Tasty lacks.
  Liquidity  — ADV ($ volume); short interest (FINRA) later.

Every function degrades to a neutral stub if candles are unavailable, so the engine never breaks.
Pure standard library + the Tasty session already configured in datafeed.py.
"""
from __future__ import annotations
import math, os
from datetime import datetime, timezone, timedelta

LOOKBACK_DAYS = int(os.environ.get("QI_LOOKBACK_DAYS", "260"))   # ~1y of daily candles
SWING = 20                                                        # swing window for S/R


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# indicator math (pure, testable — operates on plain lists)
# ---------------------------------------------------------------------------
def sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else (sum(xs) / len(xs) if xs else None)


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return 50.0
    gains = losses = 0.0
    for i in range(-n, 0):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return round(100.0 - 100.0 / (1.0 + rs), 1)


def atr(highs, lows, closes, n=14):
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(-n, 0):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    return sum(trs) / n


def _clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


# ---------------------------------------------------------------------------
# the three computed views
# ---------------------------------------------------------------------------
def technical_view(symbol: str, candles: list[dict]) -> dict:
    """candles: [{close, high, low, volume}, ...] oldest->newest. Levels/stop drive sizing & strikes."""
    base = {"schema_version": "1.0", "agent": "technical-analyst", "ticker": symbol,
            "as_of": now_iso(), "stance": "flat", "conviction": 0.0,
            "levels": {}, "signal_quality": "low", "rationale": []}
    closes = [c["close"] for c in candles if c.get("close") is not None]
    if len(closes) < 60:
        base["rationale"] = ["insufficient price history — neutral stub"]; base["_stub"] = True
        return base
    highs = [c.get("high", c["close"]) for c in candles]
    lows = [c.get("low", c["close"]) for c in candles]
    px = closes[-1]
    s50, s200 = sma(closes, 50), sma(closes, 200)
    r = rsi(closes)
    a = atr(highs, lows, closes) or (px * 0.02)
    support = min(lows[-SWING:])
    resistance = max(highs[-SWING:])
    stop = round(min(support, px - 2 * a), 2)                 # swing low or 2*ATR, whichever is lower

    up = s200 and s50 and px > s50 and s50 > s200
    down = s200 and s50 and px < s50 and s50 < s200
    if up and r < 75:
        stance, conv = "long", _clamp((px / s50 - 1) * 4 + 0.4, 0, 1)
    elif down and r > 25:
        stance, conv = "short", _clamp((1 - px / s50) * 4 + 0.4, 0, 1)
    else:
        stance, conv = "flat", 0.2
    aligned = (up or down) and 30 <= r <= 70
    sq = "high" if aligned and conv > 0.6 else ("medium" if stance != "flat" else "low")

    base.update({
        "stance": stance, "conviction": round(conv, 2),
        "levels": {"support": round(support, 2), "resistance": round(resistance, 2), "stop": stop},
        "downside_floor": stop,                                # -> Risk Manager put-write floor
        "upside_ceiling": round(resistance, 2),                # -> short-call strike reference
        "signal_quality": sq,
        "rationale": [f"px {px:.2f} vs SMA50 {s50:.2f}/SMA200 {s200:.2f}, RSI {r}, "
                      f"swing S/R {support:.2f}/{resistance:.2f}, ATR {a:.2f} -> stop {stop}"],
    })
    return base


def quant_view(symbol: str, candles: list[dict]) -> dict:
    """Price-derived factors only (momentum, low-vol). value/quality/size need fundamentals -> neutral."""
    base = {"schema_version": "1.0", "agent": "quant-analyst", "ticker": symbol,
            "as_of": now_iso(), "stance": "flat", "conviction": 0.0,
            "factors": {"value": None, "momentum": None, "quality": None, "low_vol": None,
                        "size": None, "score": 0.0}, "signal_quality": "low", "rationale": []}
    closes = [c["close"] for c in candles if c.get("close") is not None]
    if len(closes) < 130:
        base["rationale"] = ["insufficient history for factors — neutral stub"]; base["_stub"] = True
        return base
    px = closes[-1]
    # momentum: 12-1 month (skip the most recent ~21d), mapped to [-1,1]
    mom_raw = (closes[-21] / closes[-252] - 1) if len(closes) >= 252 else (closes[-21] / closes[0] - 1)
    momentum = _clamp(mom_raw / 0.30)                          # ±30% ~ full score
    # low-vol: annualized realized vol; lower vol -> positive low-vol factor
    rets = [closes[i] / closes[i - 1] - 1 for i in range(-60, 0)]
    mean = sum(rets) / len(rets)
    sd = (sum((x - mean) ** 2 for x in rets) / len(rets)) ** 0.5
    ann_vol = sd * math.sqrt(252)
    low_vol = _clamp((0.35 - ann_vol) / 0.35)                  # <35% vol -> positive
    score = round((momentum + low_vol) / 2.0, 3)
    stance = "long" if score > 0.15 else ("short" if score < -0.15 else "flat")
    floor = round(px * (1 - min(0.25, ann_vol / 2)), 2)        # vol-band downside floor
    base.update({
        "stance": stance, "conviction": round(abs(score), 2),
        "factors": {"value": None, "momentum": round(momentum, 3), "quality": None,
                    "low_vol": round(low_vol, 3), "size": None, "score": score},
        "downside_floor": floor,
        "upside_ceiling": round(px * (1 + min(0.25, ann_vol / 2)), 2),
        "signal_quality": "medium" if stance != "flat" else "low",
        "rationale": [f"momentum(12-1m) {mom_raw*100:+.1f}% -> {momentum:+.2f}; "
                      f"ann vol {ann_vol*100:.0f}% -> low_vol {low_vol:+.2f}; composite {score:+.2f} "
                      f"(value/quality/size need fundamentals — neutral)"],
    })
    return base


def liquidity_view(symbol: str, candles: list[dict], intended_pct_adv: float = 10.0) -> dict:
    """ADV + days-to-exit. Short interest (FINRA) and crowding are left null until added."""
    base = {"schema_version": "1.0", "agent": "liquidity-analyst", "ticker": symbol,
            "as_of": now_iso(), "stance": "flat", "conviction": 0.0,
            "liquidity": {"adv_usd_m": None, "days_to_exit": None, "crowding": None,
                          "short_interest_pct": None, "score": 0.0}, "rationale": []}
    rows = [c for c in candles if c.get("close") is not None and c.get("volume") is not None]
    if len(rows) < 20:
        base["rationale"] = ["insufficient volume history — neutral stub"]; base["_stub"] = True
        return base
    advs = [r["close"] * r["volume"] for r in rows[-20:]]
    adv_usd_m = round(sum(advs) / len(advs) / 1e6, 2)
    # score: ample liquidity supportive; thin liquidity trims size
    score = _clamp((math.log10(max(adv_usd_m, 0.01)) - 1.0) / 2.0)   # ~$10M/day neutral, scales up
    base.update({
        "stance": "flat", "conviction": round(abs(score), 2),
        "liquidity": {"adv_usd_m": adv_usd_m, "days_to_exit": None, "crowding": None,
                      "short_interest_pct": None, "score": round(score, 3)},
        "rationale": [f"ADV ~${adv_usd_m}M/day -> liquidity score {score:+.2f} "
                      f"(short interest via FINRA, crowding — not yet wired)"],
    })
    return base


# ---------------------------------------------------------------------------
# for the REASONING bots: factual data in, bounded numbers out
# ---------------------------------------------------------------------------
def price_context(symbol: str, candles: list[dict], swing: int = SWING) -> dict:
    """Factual price-history summary to hand a reasoning TA/Quant bot — real structure to reason over,
    not a verdict. The bot forms support/resistance/stop from these actual levels and invents nothing."""
    rows = [c for c in candles if c.get("close") is not None]
    if len(rows) < 20:
        return {"symbol": symbol.upper(), "insufficient": True}
    closes = [r["close"] for r in rows]
    highs = [r.get("high", r["close"]) for r in rows]
    lows = [r.get("low", r["close"]) for r in rows]
    px = closes[-1]
    piv_hi, piv_lo = [], []                      # swing pivots = the reaction points a structure read cares about
    for i in range(swing, len(rows) - swing):
        if highs[i] == max(highs[i - swing:i + swing + 1]):
            piv_hi.append(round(highs[i], 2))
        if lows[i] == min(lows[i - swing:i + swing + 1]):
            piv_lo.append(round(lows[i], 2))
    return {
        "symbol": symbol.upper(), "last": round(px, 2),
        "range_52w": {"high": round(max(highs), 2), "low": round(min(lows), 2)},
        "recent_high": round(max(highs[-swing:]), 2), "recent_low": round(min(lows[-swing:]), 2),
        "swing_highs": sorted(set(piv_hi))[-6:], "swing_lows": sorted(set(piv_lo))[:6],
        "last_20_closes": [round(c, 2) for c in closes[-20:]],
        "pct_from_recent_high": round((px / max(highs[-swing:]) - 1) * 100, 1),
        "pct_from_recent_low": round((px / min(lows[-swing:]) - 1) * 100, 1),
        "note": "Reason your support/resistance/stop from these real levels; do not invent prices.",
    }


def sanitize_view(view: dict, candles: list[dict]) -> dict:
    """Clamp a reasoning bot's numeric levels to the real recent price range, so a hallucinated level
    can't poison sizing or strikes. Downside levels (support/stop/floor) -> [0.9*low, last];
    upside levels (resistance/ceiling) -> [last, 1.1*high]. Notes any clamp. Returns the view."""
    rows = [c for c in candles if c.get("close") is not None]
    if len(rows) < 20 or not isinstance(view, dict):
        return view
    closes = [r["close"] for r in rows]
    highs = [r.get("high", r["close"]) for r in rows]
    lows = [r.get("low", r["close"]) for r in rows]
    last, lo, hi = closes[-1], min(lows), max(highs)
    dn_lo, dn_hi = round(lo * 0.90, 2), round(last, 2)
    up_lo, up_hi = round(last, 2), round(hi * 1.10, 2)
    clamped = []

    def bnd(x, a, b, tag):
        try:
            v = float(x)
        except (TypeError, ValueError):
            return x
        w = min(max(v, a), b)
        if abs(w - v) > 1e-6:
            clamped.append(f"{tag} {v}->{w}")
        return round(w, 2)

    lv = view.get("levels")
    if isinstance(lv, dict):
        for k, a, b in (("support", dn_lo, dn_hi), ("stop", dn_lo, dn_hi), ("resistance", up_lo, up_hi)):
            if k in lv:
                lv[k] = bnd(lv[k], a, b, k)
    if "downside_floor" in view:
        view["downside_floor"] = bnd(view["downside_floor"], dn_lo, dn_hi, "downside_floor")
    if "upside_ceiling" in view:
        view["upside_ceiling"] = bnd(view["upside_ceiling"], up_lo, up_hi, "upside_ceiling")
    if clamped:
        view.setdefault("rationale", []).append("engine bound out-of-range levels: " + "; ".join(clamped))
    return view


# ---------------------------------------------------------------------------
# Tasty candle fetch (best-effort, reuses datafeed's session) + public API
# ---------------------------------------------------------------------------
def tasty_candles(symbol: str, days: int = LOOKBACK_DAYS) -> list[dict]:
    """Daily candles from Tasty via the DXLink streamer. [] on any failure (engine then stubs)."""
    try:
        import asyncio
        from datafeed import TastytradeBackend
        from tastytrade import DXLinkStreamer
        from tastytrade.dxfeed import Candle

        be = TastytradeBackend()
        if not be.available():
            return []
        sess = be._sess()
        start = datetime.now(timezone.utc) - timedelta(days=int(days * 1.6) + 10)  # pad for non-trading days

        async def _run():
            out = []
            async with DXLinkStreamer(sess) as streamer:
                await streamer.subscribe_candle([symbol.upper()], "1d", start)
                try:
                    while True:
                        c = await asyncio.wait_for(streamer.get_event(Candle), timeout=8)
                        if getattr(c, "close", None) is not None:
                            out.append({"time": getattr(c, "time", None),
                                        "close": float(c.close),
                                        "high": float(getattr(c, "high", c.close) or c.close),
                                        "low": float(getattr(c, "low", c.close) or c.close),
                                        "volume": float(getattr(c, "volume", 0) or 0)})
                except asyncio.TimeoutError:
                    pass
            out = [r for r in out if r["time"] is not None]
            out.sort(key=lambda r: r["time"])
            return out

        return asyncio.run(_run())
    except Exception:
        return []


def build_views(symbol: str, candles: list[dict] | None = None) -> dict:
    """The three deterministic views, keyed by agent name, ready to merge into the engine's views."""
    candles = candles if candles is not None else tasty_candles(symbol)
    return {
        "technical-analyst": technical_view(symbol, candles),
        "quant-analyst": quant_view(symbol, candles),
        "liquidity-analyst": liquidity_view(symbol, candles),
    }


if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser(description="Compute deterministic TA/factor/liquidity views from Tasty.")
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--days", type=int, default=LOOKBACK_DAYS)
    ap.add_argument("--context", action="store_true",
                    help="print the factual price-history summary to feed a reasoning TA/Quant bot")
    a = ap.parse_args()
    candles = tasty_candles(a.ticker, a.days)
    if a.context:
        print(json.dumps(price_context(a.ticker, candles), indent=2))
    else:
        print(json.dumps(build_views(a.ticker, candles), indent=2))
