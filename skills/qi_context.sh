#!/usr/bin/env bash
# qi_context — real price history for one name (recent highs/lows, swing points, 52w range, last
# closes) for the Technical (Tessa) and Quant (Quilla) bots to reason over. They call this FIRST.
# Usage:  qi_context.sh TICKER
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 quant_inputs.py --ticker "$1" --context
