# Multi-Agent Example: Two Analysts → Risk Manager

This shows the **shared-state workaround** for multi-agent coordination in Hermes today.
Because subagents run isolated (no peer-to-peer talk, no shared in-memory state), the
three agents here coordinate through **structured files on disk** in `shared/`.

## The agents
Each analyst has a **role**: a *voter* helps decide the buy/sell direction; an *overlay*
only trims size or vetoes. Change an agent's role with one field in the orchestrator's
`ANALYSTS` registry.

**Voters** (directional — counted in the weighted vote):
1. **Fundamental** (`agents/fundamental-analyst.md`) — valuation → `fundamental_view.json`.
2. **Technical** (`agents/technical-analyst.md`) — price/volume → `technical_view.json`.
3. **Insider** (`agents/insider-analyst.md`) — Form 4 / 13D + revisions → `insider_view.json`.
4. **Quant / Factor** (`agents/quant-analyst.md`) — factor z-scores → `quant_view.json`.
5. **Options** (`agents/options-analyst.md`) — IV, skew, flow → `options_view.json`.
6. **Earnings Quality** (`agents/quality-analyst.md`) — forensic accounting (can veto) → `quality_view.json`.
7. **Catalyst** (`agents/catalyst-analyst.md`) — events/calendar → `catalyst_view.json`.
8. **Alt-Data** (`agents/altdata-analyst.md`) — demand nowcast → `altdata_view.json`.
9. **News** (`agents/news-analyst.md`) — curated *factual* news summary + materiality → `news_view.json` (distinct from Sentiment's crowd mood).

**Overlays** (risk lenses — trim size or veto, never set direction):
10. **Sentiment** (`agents/sentiment-analyst.md`) — social/crowd mood via MCP → `sentiment_view.json`.
11. **Macro** (`agents/macro-analyst.md`) — rate/growth/regime + a FRED/Bull-&-Bear indicator dashboard → `macro_view.json`.
12. **Credit** (`agents/credit-analyst.md`) — solvency/CDS → `credit_view.json`.
13. **Liquidity / Positioning** (`agents/liquidity-analyst.md`) — tradeability/crowding → `liquidity_view.json`.
14. **FX-Macro** (`agents/fx-macro-analyst.md`) — yen/carry-unwind & central-bank intervention risk → `fx_macro_view.json`; a fast cross-asset risk-off overlay (separate from Macro).

The **precision voters** (Fundamental, Technical, Quant) publish `signal_quality`, a
`downside_floor`, and (for shorts) an `upside_ceiling`. Fundamental also publishes a
`profit_target` (multiple × forward EPS / own research). The Risk Manager uses these for:
- `put_write` — a short-put strike *below the lowest floor* (long candidates);
- `call_write` — a bear-call credit spread *above the highest ceiling* (short candidates),
  preferring names with rich `call_convexity` from the Options Analyst.
Longs and shorts are fully symmetric: a net-negative weighted vote yields `side: short`
and bearish structures.

**Options Strategist** (`agents/options-strategist.md`, a *constructor* — not a voter or
overlay). After the Risk Manager approves a direction, `strategist.py` builds a multi-leg
structure from the fleet's views and writes `shared/structure_ticket.json`. It selects
among vertical / diagonal / calendar / double-calendar / butterfly / broken-wing / iron
condor by direction, IV rank, term structure, and event timing (one skill per family under
`skills/options-strategist/`), and distinguishes equities (American, physical) from indices
(SPX/NDX: cash-settled, European). The ticket rides the same HITL approval as the spot trade.

**Risk Manager** (`agents/risk-manager.md`) — reads ALL views, runs the weighted vote over
voters, applies every overlay, and writes `shared/risk_decision.json`.

The set of analysts is meant to grow. The orchestrator is registry-driven, so adding
an agent is one entry in `AGENTS` plus a spec file and a `*_view.json` output — no
change to the Risk Manager, which treats inputs as a list of views.

## How they "talk" (the pattern)
They never message each other. They hand off through the filesystem:

```
Fundamental Analyst ─► shared/fundamental_view.json ─┐
Technical Analyst   ─► shared/technical_view.json  ──┤
Sentiment Analyst   ─► shared/sentiment_view.json  ──┤─► Risk Manager ─► shared/risk_decision.json
Macro Analyst       ─► shared/macro_view.json      ──┤          │
   (+ future agents) ─► shared/<name>_view.json    ──┘          ▼
                                        HITL: suspend → Telegram approve/disapprove → resume
```

## Running it
`orchestrator.py` runs the whole pipeline end to end:

```
python3 orchestrator.py run --ticker NVDA
```

It authorizes the caller, runs each analyst (demo mode refreshes the sample views),
applies input + output guardrails, checks freshness and schema on every view, then
runs the Risk Manager and writes `shared/risk_decision.json`.

## Human-in-the-loop: suspend, Telegram, resume
A tradeable decision does not execute automatically. The workflow **suspends**: it
persists its state to `pending_approvals/<id>.json` (a durable store that survives a
restart) and sends a Telegram message with **APPROVE / DISAPPROVE** buttons. The
process can then exit — nothing is held in memory.

When the human taps a button, a **resume** picks up where it left off:

```
python3 orchestrator.py resume --id <approval_id> --reply approve
```

- `approve` → writes an order **ticket** to `orders/<id>.json` for the execution desk.
- `disapprove` → no order is created.

The orchestrator never places a trade or moves money; approval produces a ticket a
human executes. To send real Telegram messages, set `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID`; without them the send is a labeled dry-run. In production a Telegram
webhook maps the button tap (`approve:<id>` / `disapprove:<id>`) to the `resume` call.

See the "WHERE THE CONTROLS LIVE" header in the script for guardrails, auth, and MCP
trust placement.

- Each analyst's **structured OUTPUT** is a `view` file that conforms to
  `schemas/view.schema.json`.
- The Risk Manager's **structured INPUT** is those two files; it does not need the
  analysts' conversations, only their contracts.
- The Risk Manager's **structured OUTPUT** is `shared/risk_decision.json`, conforming
  to `schemas/risk_decision.schema.json`.

## Orchestration
An orchestrator (a supervisor agent, or a scheduled script) runs the two analysts —
in parallel or in sequence — waits for both view files to exist and be current
(`as_of` fresh enough), then runs the Risk Manager. This is the "supervisor fans out,
collects, then continues" pattern. If Hermes later ships true shared state, only the
transport changes; the **contracts stay the same** — which is exactly why you define
schemas up front.

## Why structured I/O matters here
- **Parseable, not prose.** The Risk Manager reads fields, not paragraphs, so its
  decision is deterministic and testable.
- **Cheaper.** Structured tasks need less reasoning — per your notes, a good candidate
  to route to a smaller/cheaper model.
- **A guardrail surface.** You can validate each file against its schema before the
  Risk Manager runs. A malformed or stale view is rejected, not silently trusted.
