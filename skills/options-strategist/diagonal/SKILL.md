---
name: diagonal-spread
description: >
  Build a diagonal (long back-month, short front-month at a different strike) to express a
  directional view while harvesting a rich front-month IV, typically around an event.
  Triggers: "diagonal", "poor man's covered call", "calendarized directional".
---

# Diagonal Spread

## When
Directional view + a near event with **backwardation** (front-month IV richer than back).
You want long exposure but financed by selling the expensive front month.

## Construct
- Bull: buy a back-month call ~ATM (or slightly ITM), sell a front-month call at resistance.
  Bear: mirror with puts (long back ~ATM, short front at support).
- Front expiry just after the event (captures the IV crush on the short); back ~30–40 days later.
- Net long delta (directional), net long vega (back month), positive theta from the short.

## Report
Legs (two expiries), net debit, breakeven ≈ spot ± debit, max loss ≈ net debit (back-month
retains value), max gain near the short strike at front expiry, net greeks. Roll the short
leg after the event.
