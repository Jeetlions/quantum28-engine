---
name: calendar-spread
description: >
  Build a calendar (sell front-month, buy back-month at the SAME strike) or a double
  calendar (two calendars at different strikes) to profit from front-month IV crush and
  long back-month vega around an event. Triggers: "calendar", "double calendar", "time spread".
---

# Calendar & Double Calendar

## When
Neutral-ish view + a near event with **backwardation**. You want to sell the rich front
month and own the back month's vega. A **single** calendar pins one strike (usually ATM);
a **double** calendar (put calendar at support + call calendar at resistance) widens the
profit tent when the expected move is larger.

## Construct
- Single: sell front-month option at strike K, buy back-month at the same K (ATM for a
  neutral pin). Front expiry ~ just after the event; back ~30–40 days later.
- Double: run one calendar at support and one at resistance to cover a two-sided range.
- Debit structure; net vega positive, theta positive, delta ~0 near the strike(s).

## Report
Legs (two expiries per strike), net debit (= max loss), profit tent between/around the
strikes (path-dependent), breakevens, net greeks. Avoid holding a single ATM calendar
*through* a large expected move — a big gap is the main risk.
