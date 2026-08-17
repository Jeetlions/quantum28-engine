---
name: iron-condor
description: >
  Build an iron condor (sell an OTM put spread + an OTM call spread) to collect premium on a
  range-bound view with high IV. Best on cash-settled indices. Triggers: "iron condor",
  "condor", "sell premium", "range-bound", "delta-neutral income".
---

# Iron Condor

## When
Neutral / range-bound view with **high IV rank** — you sell premium on both sides and win if
price stays inside the short strikes. Cleanest on **indices** (SPX/NDX): cash-settled and
European, so no early assignment or pin-into-shares.

## Construct
- Sell an OTM put at ~support (or spot − expected move); buy a further OTM put (define risk).
- Sell an OTM call at ~resistance (or spot + expected move); buy a further OTM call.
- Keep the short strikes OUTSIDE the expected move so a normal move doesn't breach them.
- Net credit; delta ~0, theta positive, vega negative (you're short vol).

## Report
Four legs (one expiry), net credit, max loss = width − credit, max gain = credit,
breakevens (short put − credit, short call + credit), POP ≈ 1 − credit/width. Manage at
~50% of max profit or if a short strike is tested; size for the full defined loss.
