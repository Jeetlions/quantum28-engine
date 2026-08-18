#!/usr/bin/env python3
"""
scan_watchlist.py — run the scheduled scans over watchlist.yaml.

  --bucket equities : the full 14-analyst equity vote (real mode / Hermes brain), one name at a
                      time, sequentially. Each name goes analysts -> Risk Manager -> HITL; an
                      approved trade suspends and pings you. In mock mode ($0, no key) every name
                      resolves to hold, so the scan is a safe dry-run of the whole pipeline.
  --bucket monitor  : fx + crypto. Pulls a quote and logs it — NO analysts, NO trade. These are
                      context-only names (no execution pipeline is wired for FX or crypto).

Indices (0DTE) and commodities/decay have their own scheduled jobs; they are not run here.
Observe/paper throughout: this places nothing and moves no money.
"""
from __future__ import annotations
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent


def load_watchlist() -> dict:
    import yaml
    return yaml.safe_load((BASE / "watchlist.yaml").read_text()) or {}


def scan_equities(tickers: list[str]) -> None:
    import orchestrator
    ok = 0
    for t in tickers:
        try:
            orchestrator.run(t, "scheduler", demo=False)     # real mode -> the Claude/Hermes brain
            ok += 1
        except Exception as e:                                # one bad name never stops the scan
            print(f"[scan:equities] {t}: {type(e).__name__}: {e}")
    print(f"[scan:equities] completed {ok}/{len(tickers)} names")


def monitor(tickers: list[str], label: str) -> None:
    try:
        from datafeed import DataFeed
        quotes = DataFeed().get_quotes(tickers)
    except Exception as e:
        quotes = {t: {"note": f"no feed ({e})"} for t in tickers}
    print(f"[monitor:{label}] {json.dumps(quotes)}")


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Scan the watchlist buckets (observe/paper).")
    ap.add_argument("--bucket", default="equities", choices=["equities", "monitor", "all"])
    args = ap.parse_args()
    wl = load_watchlist()
    if args.bucket in ("equities", "all"):
        scan_equities((wl.get("equities") or {}).get("tickers", []))
    if args.bucket in ("monitor", "all"):
        monitor((wl.get("fx") or {}).get("tickers", []), "fx")
        monitor((wl.get("crypto") or {}).get("tickers", []), "crypto")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
