# Porting Quantum28 to GrokBots

The split that keeps it reproducible: **GrokBots do judgment, the quant-engine does arithmetic.**
No bot computes the vote, the sizing, or the strikes — those come out of `quant_engine.py`, which
runs on the shared computer and is called through a skill-with-script. Same inputs → same ticket.

## Who becomes what (the roster, consolidated)

**Keep as GrokBots — the judgment analysts (13).** A Grok Bot is a single **description** field —
no SOUL.md / AGENTS.md, no two-file split. Paste each bot's ready-made block from
`GROKBOT_DESCRIPTIONS.md` straight in as its description:
fundamental, macro, quality, sentiment, news, catalyst, insider, alt-data, fx-macro, credit,
options (IV/skew read), technical, quant — 13 in all.
Technical and Quant reason over REAL price history handed to them (from `quant_inputs.py`), and the
engine bounds any level they return. Each writes its view JSON to `/workspace/views/<ticker>/<agent>.json`.

**Do NOT make these bots — they collapse into the one quant-engine.** The deterministic seats were
never personas; they are math, and scattering them across bot-scripts is the mistake to avoid.
Strategist, CIO/portfolio, position-manager, performance, and liquidity — this logic lives in the
Python and is reached through `quant_engine.py` (liquidity is computed by `quant_inputs.py`, since
it's a measurement, not a judgment). One engine, not many bot-scripts.

**Not a bot either — the orchestrator.** Nexus becomes two things: the **Chief of Staff** (conducts:
delegates to the analyst bots, then calls the engine, then shows you the ticket) and the engine's
`decide` (does the vote → structure → ticket). The conductor never does the numbers itself.

**Removed — instruments not tradable.** The single-stock-futures **basis book** and the levered-ETF
**decay sleeve** are dropped; their agents (`basis.py`, `decay_signals.py`, and the basis-trader /
structural-decay specs) are NOT ported.

## The quant-engine (the tool)

`quant_engine.py` — one entry point, reused modules, no model calls:

    python3 quant_engine.py decide --ticker AAPL --views views.json [--tenor 0DTE]
    python3 quant_engine.py gex    --chain chain.json

`decide` writes the ticket to `$QE_WORKSPACE/tickets/<id>.json` (default `/workspace`) and prints a
summary + JSON. Equity names run the weighted vote + MIN_CONVICTION gate + risk veto + Strategist;
index names with `--tenor 0DTE` bypass the vote and run the dealer-gamma path. Every ticket is
`awaiting_human` — never auto-executed.

## Shared-computer layout (`/workspace`)

    /workspace/
      screener.py  quant_engine.py  quant_inputs.py  portfolio.py  learning.py  strategist.py  orchestrator.py  gex_algo.py  datafeed.py ...
      universe.txt                                        # S&P500 ∪ NDX100 tickers (the screen pool)
      skills/   qe_screen.sh  qe_decide.sh  qe_gex.sh  qe_book.sh  qe_learn.sh  qi_context.sh
      views/    <ticker>/<agent>.json                     # analyst bots write here
      candidates.json                                     # the screener's shortlist + options book
      portfolio.json  book_plan.json                      # the 10-slot book + the day's reconcile plan
      tickets/  <id>.json                                 # the engine writes here
      benchmarks/  decisions.jsonl  outcomes.jsonl  analyst_weights.json   # the learning loop
      .env                                                # market-data creds (Tasty etc.)

Install deps once on the shared computer: `pip install tastytrade requests pyyaml`.

## The funnel (why the screener matters)

You can't run 13 bots on ~500 names (6,500 calls — way past Grok's quota). So:

    screener.py  (deterministic, EOD, all ~500)  ->  top-N shortlist + options book (candidates.json)
      -> the 13 reasoning bots analyze ONLY the shortlist  ->  quant_engine decide  ->  decisions
      -> portfolio.py reconciles vs the 10-slot book (enter / exit / rotate)  ->  tickets  ->  you

The screener ranks the universe on cheap Tasty signals (relative-strength momentum, volume surge,
breakout/breakdown proximity, volatility) gated by liquidity, and reports each name's up/down bias;
the bots decide direction. Tune the pass-through with `--top` (default 20) to fit your quota.

It also pulls **IV rank** (bulk, Tasty market-metrics) for the whole universe and **skew / term
structure** for the richest-IV names, then emits `options_candidates` — the single best-fit name for
each options structure (iron condor, put-write, call-credit, calendar, backspread, debit spread),
so your 30%-options book gets **at least one candidate per structure every day, with a rationale**,
handed straight to the Options structurer. (The 0DTE broken-wing legs stay on the separate 12:30
GEX path.) So `candidates.json` carries both the directional shortlist and the options book.

## The book manager (`portfolio.py`) — $10M, max 10 positions

Deterministic, no model. After `decide` produces fresh reads for the held names AND the new
candidates, `portfolio.py reconcile` proposes the day's book actions:
- **EXIT** any held name whose thesis flipped — a sell signal fires on its own merit, **even if the
  book holds only 5** (fresh read no longer long, or conviction collapsed below the exit bar).
- **ENTER** the highest-conviction new candidates into open slots (must clear `MIN_CONVICTION`).
- **ROTATE** when the book is full and an 11th candidate outranks the weakest holding — drop the
  weakest, take the stronger. Otherwise **WAITLIST** it.
Every action is a proposal you approve; the book (`portfolio.json`) only changes on `--apply`.

## The learning loop (`learning.py`) — how it gets better over time

A GrokBot is stateless and never learns on its own. This is the loop AROUND the bots that does.
Three deterministic steps:
- **log** — the engine records who voted what on every decision (`decisions.jsonl`). Automatic.
- **sync** (EOD) — opens a paper position when a name enters the book, closes it when it leaves, and
  writes the realized underlying return (`outcomes.jsonl`).
- **reweight** (weekly) — grades each bot = average of (its directional call × the trade's realized
  return) across closed outcomes, turns that into a vote weight in [0.5, 1.5] (z-scored across bots,
  EWMA-smoothed), and writes `analyst_weights.json` — which `run_risk_manager` already reads.

So a bot that keeps calling it right earns more influence; one that's consistently wrong earns less.
Weights start flat at 1.0 and only move once real outcomes accumulate — give it a few weeks of a live
book before the differentiation means anything. This is the ONLY thing that turns 13 static bots into
a system that compounds.

    ... decide (logs votes) -> portfolio -> you approve -> sync marks outcomes -> reweight -> smarter next vote

## Options structurer — the hand-off (stays deterministic)

The screener's `options_candidates` block already names, per structure, the best candidate + its IVR,
bias, and skew. **Structure construction stays in the engine** (`strategist.build_structure`, reached
by `qe_decide`) — exact strikes and widths are math, not judgment, so a bot must not freelance them
(same rule as TA levels). The hand-off is simply: read `options_candidates` → for each family, call
`qe_decide` on that name so the engine builds the named structure → ticket. If you want a
conversational "structurer" bot, its only job is to *invoke* the engine per that block, never to
compute legs itself. Put-write candidates additionally require Fibbie's soundness/valuation blessing
(see her description) before the engine writes the ticket.

## Skills to register (Settings → Plugins → Yours, per bot)

- **/qe_screen** — `skills/qe_screen.sh [TOP]` → writes `candidates.json`. Run it FIRST, at EOD.
- **/qe_decide** — `skills/qe_decide.sh TICKER VIEWS [TENOR]`. Instruction: "Call after the analyst
  bots have written their views; pass the ticker and the views file; return the ticket."
- **/qe_gex** — `skills/qe_gex.sh CHAIN`.
- **/qe_book** — `skills/qe_book.sh DECISIONS [--apply]` → reconcile the 10-slot book; run after decide.
- **/qi_context** — `skills/qi_context.sh TICKER` → real price history; enable ONLY for Trisha (Technical)
  and Quilla (Quant), who call it before reasoning. (No other bot needs a skill.)
- **/qe_learn** — `skills/qe_learn.sh sync|reweight|show` → the feedback loop (sync EOD, reweight weekly).

## Routines (replace the cron in run.py; ≤50/bot)

Schedule on the Chief of Staff:
- **EOD (after close)** → `/qe_screen` → shortlist; the 13 bots reason the shortlist; `/qe_decide`
  writes tickets; `/qe_book` reconciles the 10 slots; then `/qe_learn sync` marks outcomes.
- **12:30 ET** → `/qe_gex` + `/qe_decide --tenor 0DTE` on SPX/NDX for the broken-wing legs.
- **Weekly (e.g. Fri after close)** → `/qe_learn reweight` → updates `analyst_weights.json`.
Each routine: gather the bots' views, run the matching skill, present any `awaiting_human` ticket.

## The one rule

Judgment is the bots'. Arithmetic, construction, gates, and tickets are the engine's. Keep that line
and the pipeline stays reproducible and auditable, exactly as it was under Nexus.
