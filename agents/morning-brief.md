# Morning Brief — scheduled task (not an agent)

> **Model tier:** **Haiku 4.5** — summarizes the approval queue into the digest ($1/$5 per 1M tok).

A recurring job, not a view-producer. Before the open each weekday it (1) refreshes the
overnight news, (2) runs the pipeline across the watchlist, and (3) sends you one Telegram
message: a 2-3 sentence digest plus the day's signals awaiting your approval.

## Schedule
- Weekdays, ~07:00 in your market timezone (before the cash open).
- In Hermes: a built-in scheduled automation (cron `0 7 * * 1-5`) whose prompt runs the
  steps below and delivers to Telegram. (Hermes has native cron + messaging delivery.)

## What it does
1. **Refresh news**: have the News Analyst compile overnight / pre-market news for each
   watchlist name and the macro/FX backdrop → fresh `news_view.json`, `macro_view.json`,
   `fx_macro_view.json`.
2. **Run the pipeline**: `orchestrator.py run --ticker <T>` for each watchlist ticker.
   Each tradeable decision SUSPENDS and lands in `pending_approvals/`.
3. **Assemble the brief**: `brief.py` condenses the news into 2-3 sentences and lists the
   pending signals (ticker, side, size, conviction, any put/call-write).
4. **Deliver**: push the brief to Telegram. Each signal carries APPROVE / DISAPPROVE
   buttons (the existing HITL flow); your taps resume the workflow.

## Thresholds (so it only pings when it matters)
- Only surface signals whose |vote_net| ≥ the alert threshold and confidence ≥ medium.
- Respect the restricted list and the portfolio risk budget (CIO) before listing.
- If nothing clears, send a one-line "no signals today" so you still get the news digest.

## Disposition — nothing is lost
- Every proposal is already in `benchmarks/decision_log.jsonl` (logged at generation).
- Your APPROVE / DISAPPROVE is recorded in `benchmarks/signal_log.jsonl`.
- So the benchmark can score a **shadow book**: the trades you took vs. the ones you
  skipped — telling you over time whether your vetoes added or cost value.

## Example message
> *Quantum28 — Morning Brief (Mon 04 Aug)*
> Overnight: constructive flow on NVDA — a hyperscaler expanded its accelerator
> commitment and a supply deal was signed; one export-control headline offsets. Macro is
> risk-on with a cutting bias; FX watch — fragile JPY carry (BoJ jawboning).
> *Signals awaiting your approval (1):*
> • NVDA — LONG 1.41% NAV (aligned, conf high) · put-write 156.8P → covered call 195C
