---
name: butterfly
description: >
  Build a butterfly (long 1 / short 2 / long 1 across three equally spaced strikes) or a
  broken-wing butterfly (unequal wings) to pin a target with cheap, defined risk.
  Triggers: "butterfly", "fly", "broken wing", "BWB", "pin a target".
---

# Butterfly & Broken-Wing Butterfly

## When
You have a specific **price target** and a low-to-moderate expected move — you want a cheap,
defined-risk bet that pays best if the underlying sits near the target at expiry.

## Center on the dealer GAMMA PIN (esp. SPX/NDX)
In a **positive-gamma** regime, dealers hedge mean-reverting and price gets magnetized to the
highest-gamma strike into expiry. Center the fly on the Options Analyst's `gamma.gamma_pin`
(or `call_wall`) rather than a guessed level — that is where the peak-payoff body should sit.
Only do this when `gamma_regime == positive`; near or below `gamma_flip` the regime is
short-gamma (trending, no pin) and a fly is the wrong trade.

## Construct
- **Symmetric fly**: long 1 at (pin − width), short 2 at the pin, long 1 at (pin + width);
  width ≈ expected move. Small debit; positive theta, negative vega, ~0 delta at the pin.
- **Zero-cost broken-wing (BWB)**: use UNEQUAL wings so the structure goes on for ~zero cost
  (or a small credit): long 1 at (pin − w), short 2 at the pin, long 1 at (pin + 2w). The
  wider upper wing finances the body, so you pay ≈ nothing; risk is defined and concentrated
  on the near/broken side, with a directional lean toward the pin. Use skew (and which wall is
  nearer) to decide which wing to break.

## Reverse (short) butterfly — the breakout cousin
Invert the fly — **sell 1 wing / buy 2 body / sell 1 wing** — and it flips from a pinning trade to
a **breakout** one. Net **credit, long gamma**: it profits when price moves *away* from the center
to either wing, with **max loss at the center** and the credit kept if it expires beyond a wing.

- **When**: you expect a move but not which way, in a **negative-gamma / breakout** regime where a
  normal (long) fly is exactly wrong. It is the defined-risk, cheaper alternative to buying a straddle.
- **Vs. the ratio backspread**: the reverse fly is direction-agnostic (pays either way, capped); the
  backspread is directional (pays one way, effectively uncapped). Pick the reverse fly when the flip
  level gives you no direction; pick the backspread when it does.

## Report
Legs, net debit/credit, the two breakevens (target ± width ∓ debit), max gain (≈ width −
debit at the body; the credit for a reverse fly), max loss (the debit, the un-hedged wing distance
for a BWB, or width − credit at the center for a reverse fly), POP, and whether it is **long or
short gamma** — a normal fly pins, a reverse fly breaks out.
