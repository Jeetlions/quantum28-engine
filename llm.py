#!/usr/bin/env python3
"""
llm.py — the model "brain" for the judgment analysts (the Hermes layer, powered by Claude).

The orchestrator is the conductor: it decides WHICH analyst runs and WHEN, applies the guardrails,
runs the vote, and drives the human-in-the-loop. This module is the brain it hands one analyst at
a time: given an agent spec + a ticker, it returns a single schema-valid *view* — Claude does the
reasoning, per the per-agent model routing in config.yaml (Sonnet for judgment, Haiku for the
lighter reads; Opus is never auto-selected).

MOCK-FIRST: with no ANTHROPIC_API_KEY set (or Q28_LLM_MOCK=1), every call returns a deterministic,
NEUTRAL, schema-valid stub — so the whole pipeline runs offline and nothing breaks. Drop the key in
and it flips to live Claude calls with one variable. Live output is always validated and, on any
problem, falls back to the neutral stub, so a bad model response can never crash the fund or slip a
malformed view downstream.

Governance: this only ever sends the PUBLIC data packet + the agent's public spec to the model. It
never places a trade and never moves money — it returns a JSON view, nothing more.

Pure standard library except the (lazy, optional) `anthropic` and `yaml` imports.
"""
from __future__ import annotations
import json, os, re, time
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
AGENTS_DIR = BASE / "agents"

# ---- model routing (from config.yaml; safe defaults if absent) --------------
DEFAULT_TIERS = {
    "opus":   "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku":  "claude-haiku-4-5-20251001",
}


def _load_routing() -> tuple[dict, dict]:
    tiers, routing = dict(DEFAULT_TIERS), {}
    try:
        import yaml
        cfg = yaml.safe_load((BASE / "config.yaml").read_text()) or {}
        for k, v in (cfg.get("tiers") or {}).items():
            if v:
                tiers[k] = v
        routing = cfg.get("routing") or {}
    except Exception:
        pass
    return tiers, routing


TIERS, ROUTING = _load_routing()


def tier_for(agent_name: str) -> str:
    """sonnet | haiku | deterministic  (unrouted agents default to sonnet judgment)."""
    return ROUTING.get(agent_name, "sonnet")


def model_for(agent_name: str) -> str | None:
    t = tier_for(agent_name)
    if t == "deterministic":
        return None
    return TIERS.get(t, TIERS["sonnet"])


def live_enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and os.environ.get("Q28_LLM_MOCK", "0") != "1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# =============================================================================
# Schema-valid NEUTRAL skeletons — the mock output, and the always-safe fallback.
# Each agent's `requires` block, filled with neutral values that pass output_guardrail
# (block present; any 'score' in [-1, 1]).
# =============================================================================
SKELETON_BLOCKS: dict[str, dict] = {
    "fair_value": {},
    "levels":     {},
    "insider":    {"net_activity": "neutral", "score": 0.0},
    "factors":    {"score": 0.0},
    "options":    {"term_structure": "flat", "score": 0.0, "gamma": {}},
    "quality":    {"restatement_risk": "low", "score": 0.0},
    "catalyst":   {"score": 0.0},
    "altdata":    {"score": 0.0},
    "news":       {"materiality": "low", "score": 0.0},
    "sentiment":  {"trend": "flat", "score": 0.0},
    "macro":      {"regime": "neutral", "score": 0.0},
    "credit":     {"score": 0.0},
    "liquidity":  {"crowding": "medium", "score": 0.0},
    "fx":         {"carry_unwind_risk": "low", "score": 0.0},
}


def _skeleton(agent: dict, ticker: str, note: str) -> dict:
    block = agent["requires"]
    return {
        "schema_version": "1.0",
        "agent": agent["name"],
        "ticker": ticker,
        "as_of": _now_iso(),
        "stance": "flat",
        "conviction": 0.0,
        "confidence": "low",
        block: dict(SKELETON_BLOCKS.get(block, {"score": 0.0})),
        "rationale": [note],
        "risks": [],
    }


# =============================================================================
# Data packet — the PUBLIC inputs an analyst reasons over. MVP: options gets the
# live dealer-gamma read from the shared feed; the rest get the ticker + a note.
# Extend with per-agent fetchers (FRED for macro, Finnhub for news, EDGAR for
# fundamental, …) as the live data pipelines are built out.
# =============================================================================
def gather(agent_name: str, ticker: str) -> dict:
    data: dict = {"ticker": ticker, "as_of_utc": _now_iso()}
    try:
        if agent_name == "options-analyst":
            from datafeed import DataFeed
            feed = DataFeed()
            if feed.active() not in ("demo", "none"):
                data["options_block"] = feed.options_block(ticker)
    except Exception as e:
        data["_data_note"] = f"live data unavailable ({e})"
    if len(data) <= 2:
        data["_data_note"] = ("No dedicated live feed wired for this analyst yet — reason from the "
                              "ticker and general public knowledge; be explicit about uncertainty.")
    return data


# =============================================================================
# Prompt assembly
# =============================================================================
SCHEMA_HINT = {
    "fair_value": "fair_value:{low,base,high,current} (numbers)",
    "levels":     "levels:{support,resistance,stop} (numbers)",
    "insider":    "insider:{net_activity:buying|selling|neutral, score:-1..1}",
    "factors":    "factors:{value,momentum,quality,low_vol,size, score:-1..1}",
    "options":    "options:{iv_rank:0..100, term_structure:contango|flat|backwardation, score:-1..1}",
    "quality":    "quality:{accruals_ratio, restatement_risk:low|medium|high, score:-1..1}",
    "catalyst":   "catalyst:{type, date, expected_move_pct, score:-1..1}",
    "altdata":    "altdata:{card_spend_yoy, web_traffic_yoy, score:-1..1}",
    "news":       "news:{summary, materiality:low|medium|high, score:-1..1}",
    "sentiment":  "sentiment:{score:-1..1, mention_volume, trend:rising|flat|falling}",
    "macro":      "macro:{regime:risk_on|neutral|risk_off, rates_bias:hiking|hold|cutting, score:-1..1}",
    "credit":     "credit:{cds_bps, leverage, default_prob, score:-1..1}",
    "liquidity":  "liquidity:{adv_usd_m, days_to_exit, crowding:low|medium|high, score:-1..1}",
    "fx":         "fx:{pair, carry_unwind_risk:low|medium|high, score:-1..1}",
}

FUND_CONTEXT = (
    "Quantum28 is a systematic, defined-risk book. You are ONE analyst in a fleet; a Risk Manager "
    "reconciles all views and a human approves every trade — you never trade, you only publish a view. "
    "Use only public information. Be calibrated: a flat stance with low conviction is the right answer "
    "when the edge is unclear."
)


def _spec(agent_name: str) -> str:
    try:
        return (AGENTS_DIR / f"{agent_name}.md").read_text()[:4000]
    except Exception:
        return f"You are the {agent_name}."


def build_prompt(agent: dict, ticker: str, data: dict) -> tuple[str, str]:
    block = agent["requires"]
    system = (
        f"{_spec(agent['name'])}\n\n---\nFUND CONTEXT\n{FUND_CONTEXT}\n\n---\nOUTPUT CONTRACT\n"
        f"Return ONE JSON object and NOTHING else (no prose, no markdown fences). Required keys:\n"
        f'  schema_version:"1.0", agent:"{agent["name"]}", ticker, as_of (ISO-8601 Z),\n'
        f"  stance:long|short|flat, conviction:0..1, confidence:low|medium|high,\n"
        f"  {SCHEMA_HINT.get(block, block + ':{score:-1..1}')},\n"
        f"  rationale:[short strings], risks:[short strings].\n"
        f"stance/conviction must reflect YOUR block. Overlays (sentiment, macro, credit, liquidity, fx) "
        f"typically stay stance='flat' and express themselves through the block score. JSON only."
    )
    user = f"Ticker: {ticker}\nPublic data packet:\n{json.dumps(data, indent=2)}\n\nProduce your view now as JSON."
    return system, user


# =============================================================================
# Claude call (lazy anthropic import; prompt caching on the static system block)
# =============================================================================
def _extract_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.MULTILINE).strip()
    a, b = t.find("{"), t.rfind("}")
    if a == -1 or b == -1:
        raise ValueError("no JSON object in model output")
    return json.loads(t[a:b + 1])


def call_claude(model: str, system: str, user: str, max_tokens: int = 1200, retries: int = 3) -> dict:
    from anthropic import Anthropic
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    last = None
    for i in range(retries):
        try:
            resp = client.messages.create(
                model=model, max_tokens=max_tokens, temperature=0.2,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
            )
            return _extract_json(resp.content[0].text)
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def _coerce_valid(raw: dict, agent: dict, ticker: str) -> dict:
    """Force the fields the model must not get wrong, and validate the rest; raise if unusable."""
    block = agent["requires"]
    raw["schema_version"] = "1.0"
    raw["agent"] = agent["name"]
    raw["ticker"] = ticker
    raw["as_of"] = _now_iso()
    if raw.get("stance") not in ("long", "short", "flat"):
        raw["stance"] = "flat"
    if raw.get("confidence") not in ("low", "medium", "high"):
        raw["confidence"] = "low"
    try:
        raw["conviction"] = min(1.0, max(0.0, float(raw.get("conviction", 0.0))))
    except Exception:
        raw["conviction"] = 0.0
    if not isinstance(raw.get(block), dict):
        raise ValueError(f"missing/invalid '{block}' block")
    b = raw[block]
    if "score" in b:
        try:
            b["score"] = min(1.0, max(-1.0, float(b["score"])))
        except Exception:
            b["score"] = 0.0
    raw.setdefault("rationale", [])
    raw.setdefault("risks", [])
    return raw


# =============================================================================
# The one call the orchestrator makes.
# =============================================================================
def produce_view(agent: dict, ticker: str) -> dict:
    """Return a schema-valid view for `agent` on `ticker`. Live Claude when a key is set AND the agent
    is on an LLM tier; otherwise a neutral, schema-valid stub. Never raises — falls back to the stub."""
    if tier_for(agent["name"]) == "deterministic":
        return _skeleton(agent, ticker, "deterministic seat — computed view (stub until its engine is wired)")
    if not live_enabled():
        return _skeleton(agent, ticker, "mock mode — set ANTHROPIC_API_KEY to run this analyst live")
    model = model_for(agent["name"])
    try:
        system, user = build_prompt(agent, ticker, gather(agent["name"], ticker))
        raw = call_claude(model, system, user)
        return _coerce_valid(raw, agent, ticker)
    except Exception as e:
        view = _skeleton(agent, ticker, f"live call failed, using neutral fallback ({type(e).__name__})")
        return view


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Produce one analyst view (Claude or mock).")
    ap.add_argument("--agent", default="fundamental-analyst")
    ap.add_argument("--ticker", default="NVDA")
    args = ap.parse_args()
    agent = {"name": args.agent, "requires": {
        "fundamental-analyst": "fair_value", "technical-analyst": "levels", "insider-analyst": "insider",
        "quant-analyst": "factors", "options-analyst": "options", "quality-analyst": "quality",
        "catalyst-analyst": "catalyst", "altdata-analyst": "altdata", "news-analyst": "news",
        "sentiment-analyst": "sentiment", "macro-analyst": "macro", "credit-analyst": "credit",
        "liquidity-analyst": "liquidity", "fx-macro-analyst": "fx",
    }.get(args.agent, "factors")}
    print(f"tier={tier_for(args.agent)} model={model_for(args.agent)} live={live_enabled()}")
    print(json.dumps(produce_view(agent, args.ticker), indent=2))
