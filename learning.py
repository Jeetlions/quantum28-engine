#!/usr/bin/env python3
"""
learning.py — the feedback loop that makes the fleet get better over time.

A GrokBot is stateless; it never learns on its own. This is the loop AROUND the bots that does:
it records what each bot voted, measures how those calls actually paid off, and reweights the vote
so the bots that are right earn more influence and the ones that are wrong earn less. Three pieces,
all deterministic (no model):

  log_decision(decision, views)  — the engine calls this on every decision: who voted what.
  sync(book_tickers)             — the outcome tracker: opens a paper position when a name enters the
                                   book, closes it when it leaves, and writes the realized return.
  reweight()                     — grades each bot over a trailing window and rewrites
                                   benchmarks/analyst_weights.json, which run_risk_manager already reads.

Files (benchmarks/, next to the engine): decisions.jsonl, outcomes.jsonl, learning_positions.json,
analyst_weights.json. Pure standard library + the Tasty session in datafeed.py for marking.
"""
from __future__ import annotations
import argparse, json, os, statistics
from datetime import datetime, timezone, date
from pathlib import Path

BASE = Path(__file__).resolve().parent
BENCH = BASE / "benchmarks"                       # same dir orchestrator.load_weights reads
DECISIONS = BENCH / "decisions.jsonl"
OUTCOMES = BENCH / "outcomes.jsonl"
LEDGER = BENCH / "learning_positions.json"
WEIGHTS = BENCH / "analyst_weights.json"
PORTFOLIO = Path(os.environ.get("QE_WORKSPACE", "/workspace")) / "portfolio.json"

K = float(os.environ.get("LEARN_K", "0.5"))        # how hard performance moves weight
EWMA = float(os.environ.get("LEARN_EWMA", "0.4"))  # smoothing of the new weight vs the prior
COST_BPS = float(os.environ.get("LEARN_COST_BPS", "10"))
WMIN, WMAX = 0.5, 1.5                               # weight clamp


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sign(stance):
    return {"long": 1, "short": -1}.get((stance or "").lower(), 0)


def _read_jsonl(p):
    if not p.exists():
        return []
    out = []
    for ln in p.read_text().splitlines():
        if ln.strip():
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


def _append(p, rec):
    BENCH.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(rec) + "\n")


# --------------------------------------------------------------------------- #
# 1) the engine logs every decision: what each bot voted
# --------------------------------------------------------------------------- #
def log_decision(decision: dict, views: dict) -> None:
    """Record who voted what, so the realized outcome can later be attributed back to each bot."""
    try:
        voters = {n: {"stance": v.get("stance"), "conviction": v.get("conviction")}
                  for n, v in views.items() if isinstance(v, dict) and v.get("stance")}
        _append(DECISIONS, {
            "decision_id": f"{decision['ticker']}-{date.today().isoformat()}",
            "at": now_iso(), "ticker": decision["ticker"],
            "side": decision.get("approved_position", {}).get("side"),
            "conviction": decision.get("vote_net"),
            "voters": voters,
        })
    except Exception:
        pass                                          # logging must never break a decision


# --------------------------------------------------------------------------- #
# 2) outcome tracker: a name's realized underlying return over the time it was held
# --------------------------------------------------------------------------- #
def _price(ticker, price_fn=None):
    if price_fn:
        return price_fn(ticker)
    try:
        from datafeed import DataFeed
        q = DataFeed().get_quotes([ticker.upper()]).get(ticker.upper(), {})
        px = q.get("last") or q.get("mid") or q.get("bid")
        return float(px) if px is not None else None
    except Exception:
        return None


def _latest_decision_id(ticker):
    did = None
    for d in _read_jsonl(DECISIONS):
        if d.get("ticker") == ticker:
            did = d["decision_id"]
    return did or f"{ticker}-{date.today().isoformat()}"


def sync(book_tickers: list[str], price_fn=None) -> dict:
    """Open a paper position when a name enters the book; close it (write the realized return) when it
    leaves. `book_tickers` = the current portfolio holdings. Returns a small summary."""
    book = {t.upper() for t in book_tickers}
    led = {}
    if LEDGER.exists():
        try:
            led = json.loads(LEDGER.read_text())
        except Exception:
            led = {}
    opened = closed = 0
    today = date.today().isoformat()

    for t in book:                                    # OPEN new holdings
        if not led.get(t, {}).get("open"):
            px = _price(t, price_fn)
            if px:
                led[t] = {"decision_id": _latest_decision_id(t), "side": "long",
                          "entry_px": px, "entry_date": today, "open": True}
                opened += 1

    for t, p in list(led.items()):                    # CLOSE holdings that left the book
        if p.get("open") and t not in book:
            px = _price(t, price_fn)
            if px and p.get("entry_px"):
                ret = round((_sign(p["side"]) * (px / p["entry_px"] - 1)) * 100.0, 4)
                _append(OUTCOMES, {"decision_id": p["decision_id"], "ticker": t,
                                   "long_return_pct": round((px / p["entry_px"] - 1) * 100.0, 4),
                                   "net_return_pct": ret, "opened": p["entry_date"], "closed": today,
                                   "entry_px": p["entry_px"], "exit_px": px, "marked_at": now_iso()})
                p["open"] = False
                closed += 1

    BENCH.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(led, indent=2))
    return {"opened": opened, "closed": closed, "open_now": sum(1 for p in led.values() if p.get("open"))}


# --------------------------------------------------------------------------- #
# 3) reweight: grade each bot by how its calls paid off -> analyst_weights.json
# --------------------------------------------------------------------------- #
def reweight() -> dict:
    """Score each bot = average of (its directional call) × (the trade's realized return), across all
    closed outcomes; convert to a weight in [0.5, 1.5] (z-scored across bots), EWMA'd with the prior."""
    decisions = {d["decision_id"]: d for d in _read_jsonl(DECISIONS)}
    outcomes = _read_jsonl(OUTCOMES)
    perf = {}                                         # bot -> list of contributions
    for o in outcomes:
        d = decisions.get(o["decision_id"])
        if not d:
            continue
        R = float(o.get("long_return_pct", 0.0))
        for bot, v in d.get("voters", {}).items():
            s = _sign(v.get("stance"))
            if s == 0:
                continue
            perf.setdefault(bot, []).append(s * R - COST_BPS / 100.0)   # the bot's own call return, net

    bot_mean = {b: round(statistics.mean(xs), 3) for b, xs in perf.items() if xs}
    prior = {}
    if WEIGHTS.exists():
        try:
            prior = json.loads(WEIGHTS.read_text()).get("weights", {})
        except Exception:
            prior = {}

    weights = {}
    if len(bot_mean) >= 2:
        vals = list(bot_mean.values())
        mu = statistics.mean(vals)
        sd = statistics.pstdev(vals) or 1.0
        for b, m in bot_mean.items():
            z = (m - mu) / sd
            raw = min(WMAX, max(WMIN, 1.0 + K * z))
            weights[b] = round(EWMA * raw + (1 - EWMA) * float(prior.get(b, 1.0)), 3)
    else:
        weights = {b: float(prior.get(b, 1.0)) for b in bot_mean}       # not enough to rank yet

    rep = {"as_of": now_iso(), "n_outcomes": len(outcomes), "graded_bots": len(bot_mean),
           "per_bot_return": dict(sorted(bot_mean.items(), key=lambda kv: kv[1], reverse=True)),
           "weights": weights}
    BENCH.mkdir(parents=True, exist_ok=True)
    WEIGHTS.write_text(json.dumps(rep, indent=2))
    return rep


# --------------------------------------------------------------------------- #
# 4) scorecard: one bot's own recent record, to hand back to it for self-calibration
# --------------------------------------------------------------------------- #
PERSONA_TO_AGENT = {
    "fibbie": "fundamental-analyst", "mara": "macro-analyst", "erika": "quality-analyst",
    "seah": "sentiment-analyst", "nova": "news-analyst", "caroline": "catalyst-analyst",
    "isabelle": "insider-analyst", "sarah": "altdata-analyst", "fiona": "fx-macro-analyst",
    "carla": "credit-analyst", "olivia": "options-analyst", "trisha": "technical-analyst",
    "quilla": "quant-analyst",
}


def _agent_key(bot: str) -> str:
    b = (bot or "").strip().lower()
    return PERSONA_TO_AGENT.get(b, bot if bot.endswith("-analyst") else f"{b}-analyst")


def scorecard(bot: str, recent_n: int = 8) -> dict:
    """One bot's own realized track record — hit rate, average call return, recent calls, current
    weight — the thing the Chief of Staff hands the bot so it can calibrate its own confidence."""
    agent = _agent_key(bot)
    decisions = {d["decision_id"]: d for d in _read_jsonl(DECISIONS)}
    calls = []
    for o in _read_jsonl(OUTCOMES):
        d = decisions.get(o["decision_id"])
        if not d:
            continue
        v = d.get("voters", {}).get(agent)
        if not v:
            continue
        s = _sign(v.get("stance"))
        if s == 0:
            continue
        R = float(o.get("long_return_pct", 0.0))
        calls.append({"ticker": o.get("ticker"), "stance": v.get("stance"),
                      "call_return_pct": round(s * R, 2), "won": (s * R) > 0})
    weight = 1.0
    if WEIGHTS.exists():
        try:
            weight = float(json.loads(WEIGHTS.read_text()).get("weights", {}).get(agent, 1.0))
        except Exception:
            pass
    n = len(calls)
    hit = round(100 * sum(c["won"] for c in calls) / n, 0) if n else None
    avg = round(statistics.mean(c["call_return_pct"] for c in calls), 2) if n else None
    return {"bot": bot, "agent": agent, "graded_calls": n, "hit_rate_pct": hit,
            "avg_call_return_pct": avg, "current_weight": round(weight, 3),
            "recent": calls[-recent_n:]}


def scorecard_text(bot: str) -> str:
    s = scorecard(bot)
    if not s["graded_calls"]:
        return f"Scorecard for {bot}: no graded calls yet — no track record to calibrate from."
    rc = "; ".join(f"{c['ticker']} {c['stance']} {c['call_return_pct']:+}%" for c in s["recent"])
    return (f"Scorecard for {bot} ({s['agent']}): {s['graded_calls']} graded calls, "
            f"hit rate {s['hit_rate_pct']:.0f}%, avg call return {s['avg_call_return_pct']:+}%, "
            f"current vote weight {s['current_weight']}. Recent: {rc}.")


def main() -> int:
    ap = argparse.ArgumentParser(description="The learning loop: outcomes + reweighting + scorecards.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync", help="mark the book: open/close paper positions, write realized returns")
    sub.add_parser("reweight", help="regrade the bots and rewrite analyst_weights.json")
    sub.add_parser("show", help="print current weights + per-bot performance")
    sc = sub.add_parser("scorecard", help="one bot's own recent record (to hand back to it)")
    sc.add_argument("bot", help="persona (Fibbie) or agent key (fundamental-analyst)")
    a = ap.parse_args()

    if a.cmd == "sync":
        tickers = []
        if PORTFOLIO.exists():
            tickers = list(json.loads(PORTFOLIO.read_text()).get("positions", {}).keys())
        print("[learning] sync:", sync(tickers))
    elif a.cmd == "reweight":
        rep = reweight()
        print(f"[learning] reweight: {rep['n_outcomes']} outcomes · {rep['graded_bots']} bots graded")
        for b, w in sorted(rep["weights"].items(), key=lambda kv: kv[1], reverse=True):
            print(f"  {b:<22} weight {w}  (avg call return {rep['per_bot_return'].get(b)}%)")
    elif a.cmd == "show":
        rep = json.loads(WEIGHTS.read_text()) if WEIGHTS.exists() else {"weights": {}}
        print(json.dumps(rep, indent=2))
    elif a.cmd == "scorecard":
        print(scorecard_text(a.bot))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
