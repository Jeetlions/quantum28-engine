#!/usr/bin/env python3
"""
portfolio.py — the book manager: $10M, max 10 positions, conviction rotation, sell-signal exits.

Deterministic reconciliation (no model). Given the current book and today's fresh decisions — for
BOTH the names you hold and the new candidates — it proposes the day's book actions:

  1. EXIT any held name whose thesis flipped (a sell signal: fresh read no longer long, or conviction
     collapsed). An exit fires on its own merit — even if the book holds only 5.
  2. Fill open slots with the highest-conviction NEW candidates (ENTER).
  3. When the book is full and an extra candidate outranks the weakest holding, ROTATE: drop the
     weakest, take the stronger one. Otherwise WAITLIST it.

Every action is a PROPOSAL for your approval; the book only changes when you approve (`apply_fills`).
State lives in $QE_WORKSPACE/portfolio.json. Pure standard library.
"""
from __future__ import annotations
import argparse, json, os
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE = Path(os.environ.get("QE_WORKSPACE", "/workspace"))
BOOK = WORKSPACE / "portfolio.json"
CASH = float(os.environ.get("PF_CASH", "10000000"))
MAX_POSITIONS = int(os.environ.get("PF_MAX_POSITIONS", "10"))
ENTER_MIN_CONVICTION = float(os.environ.get("MIN_CONVICTION", "0.60"))   # a new name must clear this
EXIT_MIN_CONVICTION = float(os.environ.get("PF_EXIT_CONVICTION", "0.45"))  # held long below this = thesis gone


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_book() -> dict:
    if BOOK.exists():
        try:
            return json.loads(BOOK.read_text())
        except Exception:
            pass
    return {"cash": CASH, "max_positions": MAX_POSITIONS, "positions": {}, "updated": None}


def save_book(book: dict) -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    book["updated"] = now_iso()
    BOOK.write_text(json.dumps(book, indent=2))


def _is_sell(d: dict) -> bool:
    """A held long is sold when its fresh read is no longer long, or conviction has collapsed."""
    if not d:
        return False                                  # no fresh read -> leave it alone
    side = (d.get("side") or "").lower()
    if side in ("short", "none", "sell", "exit"):
        return True
    if d.get("decision") in ("reject",):
        return True
    if side == "long" and float(d.get("conviction", 0)) < EXIT_MIN_CONVICTION:
        return True
    return False


def reconcile(decisions: dict, book: dict | None = None) -> dict:
    """decisions: {ticker: {"side","conviction",...}} for held names AND new candidates.
    Returns the proposed book plan (no state change)."""
    book = book or load_book()
    held = book.get("positions", {})
    maxp = int(book.get("max_positions", MAX_POSITIONS))

    # 1) exits — held names whose thesis flipped (fires even if the book is under capacity)
    exits, kept = [], {}
    for tkr, pos in held.items():
        d = decisions.get(tkr)
        if _is_sell(d):
            exits.append({"ticker": tkr, "reason": "sell signal — thesis no longer long",
                          "read": d})
        else:
            conv = float((d or {}).get("conviction", pos.get("conviction", 0.0)))
            kept[tkr] = {**pos, "conviction": conv}

    # 2) new candidates (not held, fresh long clearing the entry bar), strongest first
    cands = [{"ticker": t, "conviction": float(v.get("conviction", 0))}
             for t, v in decisions.items()
             if t not in held and (v.get("side") or "").lower() == "long"
             and float(v.get("conviction", 0)) >= ENTER_MIN_CONVICTION]
    cands.sort(key=lambda c: c["conviction"], reverse=True)

    slots_free = maxp - len(kept)
    enters = cands[:max(0, slots_free)]
    overflow = cands[max(0, slots_free):]

    # 3) rotation — strongest overflow replaces the weakest holding it beats; else waitlist
    weak = sorted(kept.items(), key=lambda kv: kv[1]["conviction"])   # weakest first
    rotations, waitlist, wi = [], [], 0
    for c in overflow:
        if wi < len(weak) and weak[wi][1]["conviction"] < c["conviction"]:
            rotations.append({"out": weak[wi][0], "out_conviction": round(weak[wi][1]["conviction"], 2),
                              "in": c["ticker"], "in_conviction": round(c["conviction"], 2),
                              "reason": "higher-conviction candidate displaces the weakest holding"})
            wi += 1
        else:
            waitlist.append({"ticker": c["ticker"], "conviction": round(c["conviction"], 2),
                             "reason": "book full; does not outrank the weakest holding"})

    held_after = len(kept) + len(enters) + len(rotations)
    return {
        "as_of": now_iso(),
        "max_positions": maxp,
        "held_now": len(held), "held_after": held_after, "open_slots": maxp - len(kept),
        "exits": exits,
        "enters": [{"ticker": c["ticker"], "conviction": round(c["conviction"], 2)} for c in enters],
        "rotations": rotations,
        "waitlist": waitlist,
        "kept": sorted(kept.keys()),
    }


def apply_fills(book: dict, plan: dict, decisions: dict) -> dict:
    """Mutate the book for the actions the human APPROVED (call with the approved subset of the plan)."""
    pos = book.setdefault("positions", {})
    for e in plan.get("exits", []):
        pos.pop(e["ticker"], None)
    for r in plan.get("rotations", []):
        pos.pop(r["out"], None)
        t = r["in"]
        pos[t] = {"entry_date": now_iso(), "conviction": r.get("in_conviction"),
                  "side": "long", "source": "rotation"}
    for e in plan.get("enters", []):
        t = e["ticker"]
        pos[t] = {"entry_date": now_iso(), "conviction": e["conviction"], "side": "long", "source": "entry"}
    return book


def _print_plan(p: dict) -> None:
    print(f"[portfolio] {p['held_now']}/{p['max_positions']} held · {p['open_slots']} open → "
          f"{p['held_after']}/{p['max_positions']} after")
    for e in p["exits"]:
        print(f"  EXIT   {e['ticker']:<6} — {e['reason']}")
    for e in p["enters"]:
        print(f"  ENTER  {e['ticker']:<6} conviction {e['conviction']}")
    for r in p["rotations"]:
        print(f"  ROTATE out {r['out']} ({r['out_conviction']}) → in {r['in']} ({r['in_conviction']})")
    for w in p["waitlist"]:
        print(f"  WAIT   {w['ticker']:<6} conviction {w['conviction']} — {w['reason']}")
    if not (p["exits"] or p["enters"] or p["rotations"]):
        print("  (no book changes proposed)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Reconcile the 10-slot book against today's decisions.")
    ap.add_argument("--decisions", required=True, help="JSON {ticker:{side,conviction}} for held + candidates")
    ap.add_argument("--apply", action="store_true", help="update portfolio.json for the plan (after approval)")
    ap.add_argument("--out", default=str(WORKSPACE / "book_plan.json"))
    a = ap.parse_args()

    decisions = json.loads(Path(a.decisions).read_text())
    book = load_book()
    plan = reconcile(decisions, book)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(plan, indent=2))
    _print_plan(plan)
    if a.apply:
        save_book(apply_fills(book, plan, decisions))
        print("[portfolio] book updated (applied).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
