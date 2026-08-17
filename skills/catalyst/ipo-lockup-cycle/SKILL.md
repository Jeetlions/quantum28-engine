---
name: ipo-lockup-cycle
description: >
  Time the post-IPO lock-up cycle: recently public names often fall for ~6 months as VCs
  and insiders exit into the lock-up expiry, then quality names base. Use to phase a recent
  IPO and flag the bottoming window. Triggers: "IPO", "lock-up expiry", "new issue",
  "recently public", "post-IPO drift", "VC exit".
---

# Post-IPO Lock-up Cycle

Newly public companies carry a predictable supply schedule. This skill phases a recent IPO
and turns the lock-up calendar into a stance — but only accumulates *quality* names.

## The cycle
1. **pre_lockup** (day 0 → ~150): thin float, momentum + hype; VCs/insiders still locked.
2. **lockup_downdraft** (~day 150 → 210): the ~180-day lock-up expiry releases a large
   share supply; early holders exit; price usually drifts / sells off into and just after.
3. **bottoming_window** (~month 5 → 8): once the lock-up supply is absorbed and sellers are
   exhausted, quality names form a base (the TSLA pattern). Junk keeps falling.
4. **post**: trades on fundamentals like any other name; the cycle no longer dominates.

## Steps
1. Confirm it's a recent IPO (< ~18 months) and get the IPO date and **lock-up expiry date**
   (S-1 / 424B; expiry is typically 180 days but can be staggered or early-released).
2. Size the supply: locked shares as a % of float, VC ownership, any secondary/follow-on.
3. Set the `phase`. Stance by phase: downdraft → cautious/short-lean (negative score);
   bottoming_window → long-lean (positive score) IF the quality gate passes.
4. **Quality gate** (do not skip): require Fundamental/Quality to bless the business
   (durable revenue growth, a path to margins/FCF, clean accounting). Set
   `ipo.quality_gate` = pass / fail / unknown. On `fail`, stay away — no accumulation.
5. Confirmation: Technical must show an actual base (higher-lows, volume dry-up on
   declines); Liquidity should show the lock-up supply digested, not still hitting.

## Output
Populate `catalyst.ipo` {is_recent_ipo, ipo_date, lockup_expiry_date, phase, quality_gate}
and set the catalyst `stance`/`score` from the phase and the gate. Note the specific
lock-up date and the % of float unlocking — that is the crux of the timing.
