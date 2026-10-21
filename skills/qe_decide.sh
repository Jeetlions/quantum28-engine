#!/usr/bin/env bash
# qe_decide — Quantum28 deterministic pipeline: analyst views -> vote -> structure -> order ticket.
# Usage:  qe_decide.sh TICKER VIEWS_JSON [TENOR]
#   TICKER     e.g. AAPL, SPX
#   VIEWS_JSON path to the JSON the analyst GrokBots wrote (or - for stdin)
#   TENOR      optional, e.g. 0DTE for index same-day gamma trades
# Writes the ticket to $QE_WORKSPACE/tickets/<id>.json and prints a summary + JSON.
set -euo pipefail
cd "$(dirname "$0")/.."                 # repo root under /workspace
exec python3 quant_engine.py decide --ticker "$1" --views "$2" ${3:+--tenor "$3"}
