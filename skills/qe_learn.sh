#!/usr/bin/env bash
# qe_learn — the feedback loop that makes the fleet improve over time.
#   sync             mark the book: open/close paper positions, write realized returns   (run EOD)
#   reweight         regrade the bots from those outcomes -> analyst_weights.json         (run weekly)
#   scorecard BOT    one bot's own recent record, to hand back to it for self-calibration
#   show             print current weights + per-bot performance
# Usage:  qe_learn.sh sync | reweight | scorecard <Fibbie|...> | show
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 learning.py "${@:-reweight}"
