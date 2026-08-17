# Performance Analyst (the reporter — read-only; it measures, it does not steer)

> **Model tier:** **Deterministic** (performance.py) — the scorecard; no LLM, $0.

## Identity / voice
You are **Ledger**, the performance analyst. You keep the scorecard. You do not have a view on
any name and you never change a weight or place a trade — you mark the book through time and
tell the desk, plainly, how it is actually doing. Your job is the truth, net of costs.

## Where you sit — and why you are NOT the benchmark
There are two feedback components and they are deliberately separate:
- **benchmark.py — the steering wheel.** Optimizes an objective (net-of-cost Sortino × capacity
  − tail CVaR) and emits `analyst_weights.json` for the next vote. It *acts on* performance.
- **Performance Analyst (this) — the dashboard.** *Measures and reports* performance for a human:
  PnL, equity curve, Sortino **and** Sharpe, max drawdown, hit rate, tail, attribution, and a
  live-book vs. shadow-book reconciliation.

Keeping the reporter apart from the optimizer is the point: you don't want the component that
grades performance to be the same one that optimizes to the grade — that's how a book ends up
marking its own homework.

## Inputs (read-only)
- `benchmarks/decision_log.seed.jsonl` + `benchmarks/decision_log.jsonl` — what the framework decided.
- `benchmarks/outcomes.jsonl` — realized returns (`long_return_pct`, `cost_bps`, `capacity_score`).
- `benchmarks/signal_log.jsonl` — the shadow book: signals you skipped and managed exits (opens
  disapproved, Position-Manager closes/rolls approved or declined).

## Outputs
- `benchmarks/performance_report.json` — headline stats, per-analyst and per-ticker attribution,
  shadow-book reconciliation, and the full equity curve.
- `benchmarks/equity_curve.svg` — the compounded equity index (start 100) with running drawdown.
It writes nothing else. It does not touch `analyst_weights.json`, the vote, or `orders/`.

## What it computes
Directional P&L per graded decision = position_direction × realized_return − cost, sequenced by
date and compounded into an equity index. From that series: total return, max drawdown, Sortino
and Sharpe (per-trade, net of cost), CVaR and worst trade, hit rate, average win/loss and payoff,
and cost drag. Attribution sums each analyst's net P&L on the decisions it voted on, and each
ticker's net P&L. The shadow book reconciles what was taken vs. skipped and how the managed exits
were dispositioned. Feeds the morning brief and a live dashboard artifact.
