---
name: vertical-spread
description: >
  Build a two-leg vertical spread (bull/bear call or put spread) to express a directional
  view with defined risk. Use when direction is clear and you want capped risk. Debit when
  IV is cheap, credit when IV is rich. Triggers: "vertical", "call spread", "put spread",
  "debit spread", "credit spread".
---

# Vertical Spread

## When
Directional view, defined risk. Pick by IV regime:
- **Debit** (buy the nearer strike, sell the farther) when IV rank is LOW — you pay to be long.
- **Credit** (sell the nearer OTM strike, buy protection beyond) when IV rank is HIGH — you
  collect premium and win on time/vol.

## Construct
- Direction sets the right: bull → calls (debit) or short puts (credit); bear → mirror.
- Anchor short strikes to structure: credit spreads sell just beyond support (bull) /
  resistance (bear); debit spreads long ~ATM, short at the target (resistance / fair value).
- Width sets max risk: max_loss = width − credit (credit) or = debit (debit).

## Report
Legs, net debit/credit, breakeven (short strike ∓ credit, or long + ~0.4·width), max
loss/gain, POP ≈ 1 − credit/width (credit spreads), net delta/theta/vega.
