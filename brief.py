#!/usr/bin/env python3
"""
Morning brief — a 2-3 sentence overnight news digest + the day's signals for Telegram.

Runs before the open (scheduled). It condenses the News Analyst's overnight summary plus
the macro / FX read into a couple of sentences, then lists the signals waiting for your
APPROVE / DISAPPROVE tap. Pure standard library.

Run:  python3 brief.py            # prints the brief and writes brief/morning_brief.md
"""
from __future__ import annotations
import glob, json, os
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
SHARED = BASE / "shared"
PENDING = BASE / "pending_approvals"
OUT = BASE / "brief"


def load(p):
    p = Path(p)
    return json.loads(p.read_text()) if p.exists() else {}


def digest() -> str:
    """2-3 sentences: overnight news condensed + the macro/FX backdrop."""
    news = load(SHARED / "news_view.json").get("news", {})
    macro = load(SHARED / "macro_view.json").get("macro", {})
    fx = load(SHARED / "fx_macro_view.json").get("fx", {})
    s = []
    if news.get("summary"):
        s.append("Overnight: " + news["summary"].rstrip("."))
    if macro:
        regime = macro.get("regime", "neutral").replace("_", "-")
        bias = macro.get("rates_bias", "hold")
        s.append(f"Macro backdrop is {regime} with a {bias} bias")
    if fx.get("key_signal"):
        s.append("FX watch — " + fx["key_signal"].rstrip("."))
    return ".  ".join(s) + "." if s else "No material overnight news."


def todays_signals() -> list[dict]:
    """Signals awaiting your decision (from the durable pending-approval store)."""
    out = []
    for f in sorted(glob.glob(str(PENDING / "*.json"))):
        rec = load(f)
        if rec.get("status") != "awaiting_human":
            continue
        d = rec["decision"]; pos = d["approved_position"]
        opt = ""
        if d.get("put_write", {}).get("candidate"):
            pw = d["put_write"]; cc = pw.get("if_assigned_covered_call", {})
            opt = f" · put-write {pw['suggested_short_put_strike']}P"
            if cc: opt += f" → covered call {cc['call_strike']}C"
        if d.get("call_write", {}).get("candidate"):
            opt = f" · call-write {d['call_write']['suggested_short_call_strike']}C spread"
        out.append({
            "id": rec["approval_id"], "ticker": d["ticker"], "side": pos["side"].upper(),
            "size": pos["size_pct_nav"], "agreement": d["agreement"], "conf": d["confidence"],
            "opt": opt,
        })
    return out


def build() -> str:
    day = datetime.now(timezone.utc).strftime("%a %d %b")
    sigs = todays_signals()
    lines = [f"*Quantum28 — Morning Brief*  ({day})", "", digest(), ""]
    if sigs:
        lines.append(f"*Signals awaiting your approval ({len(sigs)}):*")
        for s in sigs:
            lines.append(f"• {s['ticker']} — {s['side']} {s['size']}% NAV "
                         f"({s['agreement']}, conf {s['conf']}){s['opt']}")
        lines.append("")
        lines.append("Reply on each with ✓ Approve / ✕ Disapprove. Skipped signals are logged.")
    else:
        lines.append("No signals cleared the threshold today — nothing awaiting approval.")
    return "\n".join(lines)


def main():
    text = build()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "morning_brief.md").write_text(text + "\n")
    print(text)
    # In production: notify_telegram(text) — reuse orchestrator.notify_telegram to push it.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
