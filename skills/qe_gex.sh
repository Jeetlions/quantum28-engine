#!/usr/bin/env bash
# qe_gex — dealer-gamma block (net GEX / flip / walls / pin) from a normalized chain.
# Usage:  qe_gex.sh CHAIN_JSON        (CHAIN_JSON = {symbol,spot,legs[]} file, or -)
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 quant_engine.py gex --chain "$1"
