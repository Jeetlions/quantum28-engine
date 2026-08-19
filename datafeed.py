#!/usr/bin/env python3
"""
Shared market-data adapter — ONE pull, one entitlement, one normalized snapshot, read by BOTH
the core Quantum28 book and the levered-ETF sleeve. No feed logic lives in either project; they
both call this.

Common interface:
    feed = DataFeed()                       # picks a backend from config, with automatic fallback
    chain = feed.get_chain("SPX")           # normalized option chain: spot + list of legs
    q     = feed.get_quotes(["BOIL","SPX"]) # {symbol: {bid, ask, last}}
    g     = feed.get_greeks("SPX")          # per-leg greeks are already in the chain; summary here
    block = feed.options_block("SPX")       # the `options` block for options_view.json (iv_rank,
                                            #   term_structure, gamma{...}) — the Analyst adds its
                                            #   own stance/conviction on top.

Backends (selected by env; automatic fallback in order):
    tastytrade  — primary  (streaming greeks + chains via dxfeed)
    ibkr        — fallback (TWS / IB Gateway; fuller chain, equities/futures/basis)
    demo        — sample data, no creds (so the pipeline runs anywhere)

Config: see .env.example. DATAFEED_PRIMARY / DATAFEED_FALLBACK pick the order.

GEX: compute_gex() is a HOOK. Drop your algorithm in as gex_algo.py (a function
     compute_gex(chain, **kw) -> gamma-block dict); until then a clearly-labeled PLACEHOLDER runs.

Personal-use data only: do not display/redistribute, and never send the raw feed to free-model
endpoints. Pure standard library (broker SDKs imported lazily inside their backends).
"""
from __future__ import annotations
import asyncio, json, math, os, threading
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
SHARED = BASE / "shared"
CONTRACT_MULTIPLIER = 100


def _load_dotenv(path: "Path | None" = None) -> None:
    """Minimal stdlib .env loader: KEY=VALUE lines -> os.environ. Anything already exported in the
    shell WINS (setdefault), so this only fills gaps. Lets `python3 datafeed.py` pick up Tasty creds
    from .env in a fresh terminal, without python-dotenv. On Railway, real env vars take precedence."""
    p = path or (BASE / ".env")
    try:
        for line in p.read_text().splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_dotenv()


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DataFeedError(Exception):
    pass


# =============================================================================
# GEX hook — YOUR algorithm goes in gex_algo.py; a placeholder ships so it runs.
# =============================================================================
try:
    from gex_algo import compute_gex as _user_gex          # you provide this file
except Exception:
    _user_gex = None


def compute_gex(chain: dict, contract_multiplier: int = CONTRACT_MULTIPLIER, **kw) -> dict:
    """Turn a normalized chain into the gamma block for options_view.json:
       {net_gex_bn, gamma_regime, gamma_flip, call_wall, put_wall, gamma_pin, computed_at}.
    If gex_algo.compute_gex exists it is used; otherwise the labeled placeholder below runs."""
    if _user_gex is not None:
        g = _user_gex(chain, contract_multiplier=contract_multiplier, **kw)
        g.setdefault("computed_at", _now_iso())
        return g
    return _placeholder_gex(chain, contract_multiplier)


def _placeholder_gex(chain: dict, mult: int = CONTRACT_MULTIPLIER) -> dict:
    """PLACEHOLDER — a naive textbook GEX so the pipeline runs. REPLACE via gex_algo.py.
    Convention here: dealers long calls / short puts; per-strike GEX = sign · gamma · OI · mult · spot².
    Your algorithm almost certainly assigns dealer sign differently and handles 0DTE — that is the IP."""
    spot = float(chain.get("spot") or 0) or 1.0
    by_strike, tot_abs = {}, {}
    for l in chain.get("legs", []):
        g = float(l.get("gamma") or 0); oi = float(l.get("open_interest") or 0); k = l["strike"]
        sign = 1 if l["right"] == "call" else -1
        by_strike[k] = by_strike.get(k, 0.0) + sign * g * oi * mult * spot * spot * 0.01
        tot_abs[k] = tot_abs.get(k, 0.0) + abs(g * oi)
    if not by_strike:
        return {"net_gex_bn": 0.0, "gamma_regime": "positive", "gamma_flip": None,
                "call_wall": None, "put_wall": None, "gamma_pin": None,
                "computed_at": _now_iso(), "_source": "PLACEHOLDER — no chain"}
    net = sum(by_strike.values()) / 1e9
    call_wall = max(by_strike, key=lambda k: by_strike[k])
    put_wall = min(by_strike, key=lambda k: by_strike[k])
    pin = max(tot_abs, key=lambda k: tot_abs[k])
    flip, cum = None, 0.0
    for k in sorted(by_strike):
        prev = cum; cum += by_strike[k]
        if (prev < 0 <= cum) or (prev > 0 >= cum):
            flip = k; break
    return {"net_gex_bn": round(net, 2), "gamma_regime": "positive" if net >= 0 else "negative",
            "gamma_flip": flip, "call_wall": call_wall, "put_wall": put_wall, "gamma_pin": pin,
            "computed_at": _now_iso(), "_source": "PLACEHOLDER — replace via gex_algo.py"}


# =============================================================================
# Backends — a common shape; each normalizes into the SAME chain format.
# Normalized chain: {symbol, spot, as_of, legs:[{expiry,dte,strike,right,bid,ask,mid,iv,
#                    delta,gamma,theta,vega,open_interest,volume}, ...]}
# =============================================================================
class Backend:
    name = "base"
    def available(self) -> bool: return False
    def get_chain(self, symbol: str) -> dict: raise NotImplementedError
    def get_quotes(self, symbols): raise NotImplementedError


def _run(coro):
    """Run an async coroutine from sync code, whether or not a loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)                     # normal: no loop yet
    box = {}                                          # already inside a loop: use a worker thread
    def _worker():
        box["v"] = asyncio.run(coro)
    t = threading.Thread(target=_worker); t.start(); t.join()
    return box["v"]


class TastytradeBackend(Backend):
    """PRIMARY. Live streaming greeks + chains via dxfeed (DXLink), authenticated with an OAuth
    personal grant (provider/client secret + refresh token — headless-friendly, no 2FA prompt).
    Session(secret, refresh_token) -> NestedOptionChain.get -> subscribe Greeks/Summary on the
    DXLinkStreamer for the strikes around ATM -> normalize into legs[]. Read-only: it subscribes to
    quotes only; it never places an order. OI comes from the Summary event, gamma/iv from Greeks."""
    name = "tastytrade"
    _session = None

    def available(self) -> bool:
        if not (os.environ.get("TASTY_CLIENT_SECRET") and os.environ.get("TASTY_REFRESH_TOKEN")):
            return False
        try:
            import tastytrade  # noqa: F401
            return True
        except Exception:
            return False

    def _sess(self):
        if TastytradeBackend._session is None:
            from tastytrade import Session
            TastytradeBackend._session = Session(
                os.environ["TASTY_CLIENT_SECRET"],
                os.environ["TASTY_REFRESH_TOKEN"],
                is_test=os.environ.get("TASTY_IS_TEST", "0") == "1",
            )
        return TastytradeBackend._session

    @staticmethod
    async def _collect(streamer, cls, symbols, timeout):
        """Pull one event of `cls` per symbol (or until timeout). dxfeed streams repeat, so we key
        by event_symbol and stop once we've seen each requested symbol once (or the clock runs out)."""
        want, got = set(symbols), {}
        loop = asyncio.get_event_loop(); end = loop.time() + timeout
        while want:
            rem = end - loop.time()
            if rem <= 0:
                break
            try:
                ev = await asyncio.wait_for(streamer.get_event(cls), rem)
            except asyncio.TimeoutError:
                break
            got[ev.event_symbol] = ev
            want.discard(ev.event_symbol)
        return got

    async def _achain(self, symbol: str, dte=None) -> dict:
        from tastytrade import DXLinkStreamer
        from tastytrade.instruments import NestedOptionChain
        from tastytrade.dxfeed import Greeks, Quote, Summary
        sess = self._sess()
        res = NestedOptionChain.get(sess, symbol)     # async in SDK >=13, sync in <=11
        if asyncio.iscoroutine(res):
            res = await res
        noc = res[0]
        exps = sorted(noc.expirations, key=lambda e: e.days_to_expiration)
        if not exps:
            raise DataFeedError(f"tastytrade: no expirations for {symbol}")
        exp = exps[0] if dte is None else min(exps, key=lambda e: abs(e.days_to_expiration - dte))
        width = float(os.environ.get("GEX_STRIKE_WIDTH_PCT", "0.10"))
        timeout = float(os.environ.get("TASTY_STREAM_TIMEOUT", "8"))

        async with DXLinkStreamer(sess) as streamer:
            # 1) underlying spot from a Quote (env override wins; else fall back to the middle strike)
            spot, spot_est = None, False
            env_spot = os.environ.get(f"TASTY_SPOT_{symbol.upper()}")
            if env_spot:
                spot = float(env_spot)
            else:
                await streamer.subscribe(Quote, [symbol])
                try:
                    q = await asyncio.wait_for(streamer.get_event(Quote), timeout)
                    bid, ask = getattr(q, "bid_price", None), getattr(q, "ask_price", None)
                    if bid and ask:
                        spot = (float(bid) + float(ask)) / 2
                except asyncio.TimeoutError:
                    pass
            strikes = exp.strikes
            if spot is None:
                spot = float(strikes[len(strikes) // 2].strike_price); spot_est = True

            # 2) select strikes within ±width of spot, gather their call/put streamer symbols
            lo, hi = spot * (1 - width), spot * (1 + width)
            sel = [s for s in strikes if lo <= float(s.strike_price) <= hi]
            sym_map = []          # (strike, right, streamer_symbol)
            for s in sel:
                if s.call_streamer_symbol:
                    sym_map.append((float(s.strike_price), "call", s.call_streamer_symbol))
                if s.put_streamer_symbol:
                    sym_map.append((float(s.strike_price), "put", s.put_streamer_symbol))
            allsyms = [m[2] for m in sym_map]
            if not allsyms:
                raise DataFeedError(f"tastytrade: no strikes within ±{width:.0%} of {spot} for {symbol}")

            # 3) stream greeks (gamma/iv/delta) + summary (open interest)
            await streamer.subscribe(Greeks, allsyms)
            await streamer.subscribe(Summary, allsyms)
            greeks = await self._collect(streamer, Greeks, allsyms, timeout)
            summ = await self._collect(streamer, Summary, allsyms, timeout)

        legs = []
        exp_iso, dte_v = exp.expiration_date.isoformat(), exp.days_to_expiration
        for k, right, sym in sym_map:
            g, sm = greeks.get(sym), summ.get(sym)
            if g is None and sm is None:
                continue                                 # no data (illiquid / halted) — skip
            gv = lambda o, a: getattr(o, a, None) if o is not None else None
            iv, delta, gamma = gv(g, "volatility"), gv(g, "delta"), gv(g, "gamma")
            theta, vega, oi = gv(g, "theta"), gv(g, "vega"), gv(sm, "open_interest")
            legs.append({
                "expiry": exp_iso, "dte": dte_v, "strike": k, "right": right,
                "bid": None, "ask": None, "mid": None,
                "iv": float(iv) if iv is not None else None,
                "delta": float(delta) if delta is not None else None,
                "gamma": float(gamma) if gamma is not None else None,
                "theta": float(theta) if theta is not None else None,
                "vega": float(vega) if vega is not None else None,
                "open_interest": int(oi) if oi is not None else 0,
                "volume": None,
            })
        return {"symbol": symbol.upper(), "spot": round(float(spot), 2), "as_of": _now_iso(),
                "legs": legs, "_spot_estimated": spot_est}

    async def _aquotes(self, symbols) -> dict:
        from tastytrade import DXLinkStreamer
        from tastytrade.dxfeed import Quote
        timeout = float(os.environ.get("TASTY_STREAM_TIMEOUT", "8"))
        async with DXLinkStreamer(self._sess()) as streamer:
            await streamer.subscribe(Quote, list(symbols))
            got = await self._collect(streamer, Quote, list(symbols), timeout)
        out = {}
        for s in symbols:
            q = got.get(s)
            bid, ask = (getattr(q, "bid_price", None), getattr(q, "ask_price", None)) if q else (None, None)
            if bid and ask:
                bid, ask = float(bid), float(ask)
                out[s] = {"bid": bid, "ask": ask, "last": round((bid + ask) / 2, 4)}
            else:
                out[s] = {"bid": None, "ask": None, "last": None}
        return out

    def get_chain(self, symbol: str, dte=None) -> dict:
        return _run(self._achain(symbol.upper(), dte))

    def get_quotes(self, symbols):
        return _run(self._aquotes([s.upper() for s in symbols]))


def _f(v):
    """float-or-None, treating NaN and non-positive as missing."""
    try:
        v = float(v)
        return v if (v == v and v > 0) else None
    except Exception:
        return None


def _first_px(t):
    """Best available price from an ib_async Ticker: last -> close -> mid."""
    for v in (getattr(t, "last", None), getattr(t, "close", None)):
        p = _f(v)
        if p is not None:
            return p
    b, a = _f(getattr(t, "bid", None)), _f(getattr(t, "ask", None))
    return round((b + a) / 2, 4) if (b and a) else None


class IBKRBackend(Backend):
    """Interactive Brokers via IB Gateway / TWS (ib_async). Its job in this fleet is the pieces
    Tastytrade does NOT give: the VIX / nat-gas / crude FUTURES CURVES (front vs second month) that
    the decay engine needs, plus equity/ETF quotes and (later) SSF basis + borrow. Option chains and
    greeks stay on Tastytrade. Read-only: it only requests market data, never places an order.
    IB Gateway must be running and reachable at IB_HOST:IB_PORT (4002 = paper gateway)."""
    name = "ibkr"
    _ib = None

    def available(self) -> bool:
        if os.environ.get("IB_ENABLED", "0") != "1":
            return False
        try:
            import ib_async  # noqa: F401  (maintained fork of ib_insync)
            return True
        except Exception:
            return False

    def _connect(self):
        from ib_async import IB
        ib = IBKRBackend._ib
        if ib is not None and ib.isConnected():
            return ib
        ib = IB()
        ib.connect(os.environ.get("IB_HOST", "127.0.0.1"),
                   int(os.environ.get("IB_PORT", "4002")),
                   clientId=int(os.environ.get("IB_CLIENT_ID", "17")),
                   timeout=int(os.environ.get("IB_TIMEOUT", "15")),
                   readonly=True)
        # If real-time isn't entitled for a contract, fall back to delayed rather than nothing.
        try:
            ib.reqMarketDataType(int(os.environ.get("IB_MKT_DATA_TYPE", "1")))   # 1 live, 3 delayed
        except Exception:
            pass
        IBKRBackend._ib = ib
        return ib

    def disconnect(self):
        if IBKRBackend._ib is not None and IBKRBackend._ib.isConnected():
            IBKRBackend._ib.disconnect()
        IBKRBackend._ib = None

    def get_quotes(self, symbols):
        from ib_async import Stock
        ib = self._connect()
        contracts = [Stock(s.upper(), "SMART", "USD") for s in symbols]
        ib.qualifyContracts(*contracts)
        tickers = ib.reqTickers(*contracts)
        out = {}
        for s, t in zip(symbols, tickers):
            out[s.upper()] = {"bid": _f(getattr(t, "bid", None)),
                              "ask": _f(getattr(t, "ask", None)),
                              "last": _first_px(t)}
        return out

    def futures_curve(self, root: str, exchange: str, n: int = 2) -> dict:
        """Front/second (or more) listed futures for a root -> {front, second, ratio}. The RATIO
        (front/second) is the decay engine's curve signal: <1 contango, >1 backwardation. Roots:
        VIX on CFE, NG and CL on NYMEX."""
        from ib_async import Future
        ib = self._connect()
        cds = ib.reqContractDetails(Future(root, exchange=exchange))
        contracts = sorted((cd.contract for cd in cds),
                           key=lambda c: c.lastTradeDateOrContractMonth)[:n]
        if not contracts:
            return {"root": root, "front": None, "second": None, "ratio": None, "note": "no contracts"}
        ib.qualifyContracts(*contracts)
        prices = [_first_px(t) for t in ib.reqTickers(*contracts)]
        front, second = (prices + [None, None])[:2]
        ratio = round(front / second, 4) if (front and second) else None
        return {"root": root, "exchange": exchange, "front": front, "second": second,
                "ratio": ratio, "front_expiry": contracts[0].lastTradeDateOrContractMonth,
                "regime": (None if ratio is None else ("backwardation" if ratio > 1.0 else "contango")),
                "as_of": _now_iso()}

    def get_chain(self, symbol: str) -> dict:
        # Option chains + greeks are Tastytrade's job (streaming dxfeed). IB is here for futures
        # curves, quotes, basis and borrow — not chains.
        raise DataFeedError("IB backend does not serve option chains — Tastytrade is primary for those")


class DemoBackend(Backend):
    """Sample data so the whole pipeline runs with no creds. Synthesizes a plausible chain
    (ATM-peaked gamma, OI, a vol smile, two expiries) per symbol."""
    name = "demo"
    SPOTS = {"SPX": 5960.0, "NDX": 21500.0, "BOIL": 12.0, "KOLD": 34.0, "UVXY": 18.5,
             "TQQQ": 78.0, "SOXL": 41.0, "NVDA": 182.0, "AAPL": 232.0}
    def available(self) -> bool: return True
    def get_chain(self, symbol: str) -> dict:
        spot = self.SPOTS.get(symbol.upper(), 100.0)
        inc = 5 if spot > 500 else (1 if spot > 40 else 0.5)
        legs = []
        for dte, base_iv in ((7, 0.14), (35, 0.16)):           # front + back expiry
            for n in range(-6, 7):
                k = round((round(spot / inc) + n) * inc, 2)
                m = abs(k - spot) / spot
                iv = round(base_iv + 0.9 * m + (0.02 if k < spot else 0.0), 4)   # smile + slight put skew
                gamma = round(math.exp(-((k - spot) ** 2) / (2 * (0.03 * spot) ** 2)) / (spot * iv * math.sqrt(dte / 365)) * 0.01, 6)
                oi = int(4000 * math.exp(-((k - spot) ** 2) / (2 * (0.04 * spot) ** 2)) + 50)
                for right in ("call", "put"):
                    d = 0.5 * (1 if right == "call" else -1) * math.exp(-4 * m)
                    legs.append({"expiry": f"+{dte}d", "dte": dte, "strike": k, "right": right,
                                 "bid": None, "ask": None, "mid": None, "iv": iv,
                                 "delta": round(d, 3), "gamma": gamma, "theta": None, "vega": None,
                                 "open_interest": oi, "volume": None})
        return {"symbol": symbol.upper(), "spot": spot, "as_of": _now_iso(), "legs": legs}
    def get_quotes(self, symbols):
        return {s.upper(): {"bid": None, "ask": None, "last": self.SPOTS.get(s.upper(), 100.0)} for s in symbols}


# =============================================================================
# DataFeed — config-driven selection with AUTOMATIC FALLBACK across backends.
# =============================================================================
def _build_backends():
    reg = {"tastytrade": TastytradeBackend(), "ibkr": IBKRBackend(), "demo": DemoBackend()}
    order = [os.environ.get("DATAFEED_PRIMARY", "tastytrade"),
             os.environ.get("DATAFEED_FALLBACK", "ibkr"), "demo"]
    seen, out = set(), []
    for n in order:
        if n in reg and n not in seen:
            out.append(reg[n]); seen.add(n)
    return out


class DataFeed:
    def __init__(self):
        self.backends = _build_backends()

    def active(self) -> str:
        for b in self.backends:
            if b.available():
                return b.name
        return "none"

    def _try(self, method: str, *args):
        errs = []
        for b in self.backends:
            if not b.available():
                continue
            try:
                return getattr(b, method)(*args), b.name
            except Exception as e:
                errs.append(f"{b.name}: {e}")
        raise DataFeedError(f"all backends failed for {method}{args}: " + " | ".join(errs))

    def get_chain(self, symbol: str) -> dict:
        chain, src = self._try("get_chain", symbol)
        chain["_source"] = src
        return chain

    def get_quotes(self, symbols):
        q, _ = self._try("get_quotes", list(symbols))
        return q

    def get_greeks(self, symbol: str) -> dict:
        chain = self.get_chain(symbol)
        return {l["strike"]: {"right": l["right"], "delta": l["delta"], "gamma": l["gamma"],
                              "iv": l["iv"], "open_interest": l["open_interest"]} for l in chain["legs"]}

    # ---- the normalized `options` block the Options Analyst consumes -----------
    def options_block(self, symbol: str, tenor: str | None = None) -> dict:
        chain = self.get_chain(symbol)
        spot = chain["spot"]
        atm = min(chain["legs"], key=lambda l: abs(l["strike"] - spot))
        fronts = [l["iv"] for l in chain["legs"] if l["dte"] == min(x["dte"] for x in chain["legs"]) and abs(l["strike"] - spot) < spot * 0.02]
        backs = [l["iv"] for l in chain["legs"] if l["dte"] == max(x["dte"] for x in chain["legs"]) and abs(l["strike"] - spot) < spot * 0.02]
        f, b = (sum(fronts) / len(fronts) if fronts else 0), (sum(backs) / len(backs) if backs else 0)
        term = "contango" if b > f * 1.02 else ("backwardation" if f > b * 1.02 else "flat")
        calls = sum(l["open_interest"] for l in chain["legs"] if l["right"] == "call")
        puts = sum(l["open_interest"] for l in chain["legs"] if l["right"] == "put")
        gblock = compute_gex(chain)
        gblock.setdefault("spot", spot)         # carry the real underlying so 0DTE centers correctly
        return {
            "iv": round(atm["iv"], 4),
            "iv_rank": None,                    # TODO: needs an IV history series (add a store)
            "term_structure": term,
            "put_call": round(puts / calls, 2) if calls else None,
            "tenor": tenor,
            "spot": spot,                       # real underlying price (0DTE strike centering)
            "gamma": gblock,                    # <-- your algorithm (or placeholder); also carries spot
            "source": chain["_source"], "as_of": chain["as_of"],
        }


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Shared market-data adapter (tastytrade / ibkr / demo).")
    ap.add_argument("--symbol", default="SPX")
    ap.add_argument("--tenor", default=None)
    ap.add_argument("--write", action="store_true", help="write shared/options_data_<sym>.json")
    args = ap.parse_args()
    feed = DataFeed()
    print(f"[datafeed] active backend: {feed.active()}  (order: {[b.name for b in feed.backends]})")
    block = feed.options_block(args.symbol, tenor=args.tenor)
    print(json.dumps(block, indent=2))
    if args.write:
        SHARED.mkdir(parents=True, exist_ok=True)
        (SHARED / f"options_data_{args.symbol.upper()}.json").write_text(json.dumps(block, indent=2))
        print(f"-> shared/options_data_{args.symbol.upper()}.json")
    if block["gamma"].get("_source", "").startswith("PLACEHOLDER"):
        print("\nNOTE: GEX is the PLACEHOLDER. Drop your algorithm in gex_algo.py "
              "(def compute_gex(chain, **kw) -> gamma block) and it will be used automatically.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
