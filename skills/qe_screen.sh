#!/usr/bin/env bash
# qe_screen — EOD candidate selection: rank S&P500+NDX100 on Tasty signals -> today's shortlist.
# Writes $QE_WORKSPACE/candidates.json and prints the ranked list. Run it EOD, before the bots.
# Usage:  qe_screen.sh [TOP]        (TOP = how many names to pass through; default 20)
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 screener.py ${1:+--top "$1"}
