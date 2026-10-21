# Shared-Computer Setup — getting the engine live on Grok

The bots are built. This wires the deterministic engine onto Grok's shared computer so Gian Myles
can actually call it. Do Part A once on your Mac; delegate Parts B–C to Gian Myles (it has a
terminal); Part D is the go-live test. Paste steps to Gian Myles as needed.

---

## Part A — push the engine to GitHub (on your Mac, once)

1. Open Terminal and run:

       cd "/Users/jeetsingh/Desktop/Patterns of AI Agents/Multi-Agent Risk Example"
       git add -A
       git commit -m "GrokBot engine: screener, quant-engine, portfolio, learning, skills"
       git push

2. If the repo is PRIVATE, Gian Myles's clone will need a read-only access token — either create one,
   or skip cloning and upload the files to /workspace directly (Part B, alt).

---

## Part B — install the engine (delegate to Gian Myles)

Tell Gian Myles: "Set up the engine on your computer."

3. Clone (or pull) the repo into the workspace:

       git clone https://github.com/Quantum28Cap/Quantum28-Capital.git /workspace/q28
       cd /workspace/q28

   (ALT, no GitHub: upload these files into /workspace/q28 instead —
    screener.py, quant_engine.py, quant_inputs.py, portfolio.py, learning.py, strategist.py,
    orchestrator.py, gex_algo.py, datafeed.py, universe.txt, and the skills/ folder.)

4. Install the dependencies:

       pip install tastytrade requests pyyaml

5. Point the engine's outputs at this folder:

       export QE_WORKSPACE=/workspace/q28        # add to the shell profile so it persists

---

## Part C — credentials (you provide; Gian Myles saves)

6. Create `/workspace/q28/.env` with your Tastytrade OAuth credentials:

       DATAFEED_PRIMARY=tastytrade
       TASTY_CLIENT_SECRET=<your OAuth client secret>
       TASTY_REFRESH_TOKEN=<your personal OAuth refresh token>
       # optional tuning:
       # MIN_CONVICTION=0.60
       # SCR_TOP=20
       # PF_CASH=10000000
       # PF_MAX_POSITIONS=10

   Never paste these into chat; put them in the file on the shared computer.

---

## Part D — register the skills (in Grok · Settings → Plugins → Yours)

7. Create one skill per wrapper, each running the matching script in /workspace/q28/skills/:

       /qe_screen   ->  bash skills/qe_screen.sh [TOP]
       /qe_decide   ->  bash skills/qe_decide.sh TICKER VIEWS [TENOR]
       /qe_gex      ->  bash skills/qe_gex.sh CHAIN
       /qe_book     ->  bash skills/qe_book.sh DECISIONS [--apply]
       /qe_learn    ->  bash skills/qe_learn.sh sync|reweight|scorecard <Bot>|show
       /qi_context  ->  bash skills/qi_context.sh TICKER

8. Enable the first five (qe_*) on **Gian Myles** only.
9. Enable **/qi_context** on **Trisha** and **Quilla** only. No other bot gets a skill.

---

## Part E — go-live test (the proof Tasty is flowing)

10. Ask Gian Myles to run `/qe_screen`. Expected: it writes `candidates.json` and prints a ranked
    list of REAL tickers (directional shortlist + the options book), not an empty result.
    - Empty / all-stub result  ->  the Tasty fetch isn't working (creds, or the candle/market-metrics
      field names differ in your SDK version). Send me the output and it's a one-line fix.
11. Then a single name end to end: have Gian Myles gather the 13 analysts' reads on the top candidate,
    convert them to the view JSON, and run `/qe_decide TICKER <views>`. Confirm a ticket comes back.

---

## Part F — routines (on Gian Myles, once the test passes)

12. EOD (after close): `/qe_screen` → bots reason the shortlist → `/qe_decide` per name →
    `/qe_book` → `/qe_learn sync`.
13. 12:30 p.m. ET: `/qe_gex` + `/qe_decide --tenor 0DTE` on SPX/NDX.
14. Friday after close: `/qe_learn reweight`.

---

## Reminders

- Judgment is the bots'; arithmetic is the engine's. Gian Myles converts each analyst's prose into the
  view JSON — and maps a hedged macro read ("leaning risk-off") to a NEUTRAL regime + the score, never
  to full `risk_off` (which vetoes every long). That translation discipline is the key operational detail.
- Weights and scorecards start flat and only move after a few weeks of closed trades — that's correct.
- Every ticket is `awaiting_human`. Nothing auto-executes.
