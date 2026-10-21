#!/usr/bin/env bash
# qe_book — reconcile the $10M / 10-slot book against today's decisions: enter / exit / rotate.
# Proposals only; pass --apply (after you approve) to update portfolio.json.
# Usage:  qe_book.sh DECISIONS_JSON [--apply]
#   DECISIONS_JSON: {"TICKER":{"side":"long|short|none","conviction":0.x}, ...} for held names + candidates
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 portfolio.py --decisions "$1" ${2:-}
