---
name: ratio-spread
description: >
  Build a ratio spread — a ratio BACKSPREAD (net long options, long gamma) for a breakout, or a
  DEFINED front ratio (net short, short gamma) for directional income. The backspread is the
  go-to long-gamma expression when a negative-gamma / breakout regime vetoes a pinning fly.
  Triggers: "ratio spread", "backspread", "ratio backspread", "front ratio", "1x2", "breakout long gamma".
---

# Ratio Spreads

A ratio spread trades an unequal number of long and short options at two strikes (classically 1x2).
Which way the ratio runs flips the entire character of the trade.

## Ratio BACKSPREAD — long gamma, for a breakout
Sell 1 near-the-money, buy 2 further out toward the break. You are **net long options**, so the
structure is **long gamma / long vega, ~zero cost**, with a small **defined** loss if price stalls
between the strikes and a large gain on a fast directional move.

- **When**: a breakout regime — a **negative-gamma** dealer read (dealers amplify the move), or a
  strong directional view with **cheap IV** (low IV rank) and a big expected move. This is the
  long-gamma expression to flip to when a pinning fly is the wrong trade.
- **Construct**: bullish → *call* backspread (sell 1 at spot, buy 2 toward the call wall / target);
  bearish → *put* backspread (sell 1 at spot, buy 2 toward the put wall). Aim for ~zero net cost.
- **Risk**: max loss ≈ the strike width, realized only if it expires at the long strike; profit is
  effectively uncapped on a fast break. Negative theta — it needs the move to come.

## Front RATIO — short gamma income, made DEFINED
Buy 1, sell 2 at the target, **buy 1 protective wing** beyond. Net **short options** → **short
gamma / short vega, credit**, best if price drifts to the short strikes. The classic 1x2 front ratio
has an uncapped tail past the shorts; the protective wing converts that into **defined risk** (and,
taken to equal spacing, becomes the broken-wing butterfly — so keep the shorts offset to stay a ratio).

- **When**: a mild directional lean into **rich IV** (high IV rank), expecting a drift toward a target,
  not a violent move. Income with a lean.
- **Risk**: defined by the wing. Positive theta, short vega — the mirror of the backspread.

## Report
Legs with quantities (the 1x2 is the point), net debit/credit, breakevens, max loss/gain, POP, and
net greeks — and state plainly whether the structure is **long gamma (breakout)** or **short gamma
(income)**, because that is the decision the ratio encodes.
