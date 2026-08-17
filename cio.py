#!/usr/bin/env python3
"""
CIO / Portfolio Manager — the portfolio-level layer ABOVE the per-name Risk Manager.

The Risk Manager decides ONE name at a time (direction, size, veto). The CIO takes the
book of approved per-name proposals and makes them coherent as a PORTFOLIO: it enforces
gross/net exposure caps, per-name and per-sector limits, correlation-cluster caps, and a
single total risk budget — scaling positions down (pro-rata) until every constraint holds.
It writes portfolio/portfolio_decision.json (the approved book) and an audit of what it cut.

This is the multi-name step. Each proposal is one Risk Manager decision; the CIO is the
coordinator over many of them. Pure standard library.

Run:  python3 cio.py --book portfolio/sample_book.json
"""
from __future__ import annotations
import argparse, json, os, sys, tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT = BASE / "portfolio"

# ---- portfolio constraints (all in % of NAV) --------------------------------
CFG = {
    "max_gross_pct": 12.0,      # sum of |position sizes|
    "max_net_pct": 8.0,         # sum of signed sizes (abs)
    "max_per_name_pct": 3.0,    # any single position
    "max_per_sector_pct": 3.5,  # sum of |sizes| within a sector
    "max_per_cluster_pct": 3.5, # sum of |sizes| within a correlation cluster
    "risk_budget_pct": 3.0,     # sum of modeled max_loss across the book
}
MIN_TICKET_PCT = 0.25          # a slice smaller than this isn't worth a book slot
ROTATE_EDGE = 0.05             # a NEW name must out-convict a HELD one by this to displace it (anti-churn)
BOOKS = Path(__file__).resolve().parent / "books"


def load_book(name):
    """Load a book/sleeve config: its OWN caps, max_positions, universe, tag.
    Each book is a separate portfolio with its own risk budget (e.g. the levered-ETF sleeve)."""
    f = BOOKS / f"{name}.json"
    if not f.exists():
        raise SystemExit(f"no book config at {f}")
    b = json.loads(f.read_text())
    cfg = dict(CFG); cfg.update(b.get("caps", {}))
    return {"cfg": cfg, "max_positions": b.get("max_positions"), "universe": b.get("universe"),
            "tag": b.get("tag", name), "style": b.get("style", ""), "constraints": b.get("constraints", {})}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_json(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def _signed(p):
    return p["approved_pct"] * (1 if p["side"] == "long" else -1)


def _scale_group(positions, key_fn, cap, actions, label):
    """Scale every group whose summed |size| exceeds cap down to the cap, pro-rata."""
    groups = defaultdict(list)
    for p in positions:
        groups[key_fn(p)].append(p)
    for g, members in groups.items():
        if g is None:
            continue
        gross = sum(m["approved_pct"] for m in members)
        if gross > cap + 1e-9:
            factor = cap / gross
            for m in members:
                m["approved_pct"] = round(m["approved_pct"] * factor, 3)
            actions.append(f"{label} '{g}' at {gross:.2f}% > {cap:.2f}% cap -> scaled x{factor:.2f}")


def build_portfolio(proposals, cfg=CFG):
    # start from each name's proposed size
    book = []
    for p in proposals:
        book.append({
            "ticker": p["ticker"], "side": p["side"], "sector": p.get("sector", "unknown"),
            "cluster": p.get("cluster"), "proposed_pct": p["size_pct_nav"],
            "approved_pct": p["size_pct_nav"],
            "max_loss_frac": p.get("max_loss_pct_nav", 0) / max(p["size_pct_nav"], 1e-9),
        })
    actions = []

    # 1) per-name cap
    for p in book:
        if p["approved_pct"] > cfg["max_per_name_pct"]:
            actions.append(f"{p['ticker']} {p['approved_pct']:.2f}% > per-name cap {cfg['max_per_name_pct']}% -> capped")
            p["approved_pct"] = cfg["max_per_name_pct"]

    # 2) per-sector and 3) per-correlation-cluster caps
    _scale_group(book, lambda p: p["sector"], cfg["max_per_sector_pct"], actions, "sector")
    _scale_group(book, lambda p: p["cluster"], cfg["max_per_cluster_pct"], actions, "cluster")

    # 4) gross exposure cap (scale the whole book)
    gross = sum(p["approved_pct"] for p in book)
    if gross > cfg["max_gross_pct"] + 1e-9:
        f = cfg["max_gross_pct"] / gross
        for p in book:
            p["approved_pct"] = round(p["approved_pct"] * f, 3)
        actions.append(f"gross {gross:.2f}% > {cfg['max_gross_pct']}% -> whole book scaled x{f:.2f}")

    # 5) net exposure cap (trim the heavier side)
    net = sum(_signed(p) for p in book)
    if abs(net) > cfg["max_net_pct"] + 1e-9:
        heavy = "long" if net > 0 else "short"
        side_sum = sum(p["approved_pct"] for p in book if p["side"] == heavy)
        target = side_sum - (abs(net) - cfg["max_net_pct"])
        f = max(0.0, target / side_sum)
        for p in book:
            if p["side"] == heavy:
                p["approved_pct"] = round(p["approved_pct"] * f, 3)
        actions.append(f"net {net:+.2f}% exceeds ±{cfg['max_net_pct']}% -> {heavy} side scaled x{f:.2f}")

    # 6) total risk budget (sum of modeled max loss)
    risk = sum(p["approved_pct"] * p["max_loss_frac"] for p in book)
    if risk > cfg["risk_budget_pct"] + 1e-9:
        f = cfg["risk_budget_pct"] / risk
        for p in book:
            p["approved_pct"] = round(p["approved_pct"] * f, 3)
        actions.append(f"portfolio risk {risk:.2f}% > budget {cfg['risk_budget_pct']}% -> book scaled x{f:.2f}")

    # recompute final exposures
    gross = round(sum(p["approved_pct"] for p in book), 2)
    net = round(sum(_signed(p) for p in book), 2)
    risk = round(sum(p["approved_pct"] * p["max_loss_frac"] for p in book), 2)
    by_sector = defaultdict(float)
    for p in book:
        by_sector[p["sector"]] += p["approved_pct"]

    for p in book:
        p["approved_pct"] = round(p["approved_pct"], 2)
        p.pop("max_loss_frac", None)

    return {
        "agent": "portfolio-cio", "as_of": now_iso(),
        "constraints": cfg,
        "book": book,
        "exposures": {"gross_pct": gross, "net_pct": net,
                      "by_sector": {k: round(v, 2) for k, v in by_sector.items()}},
        "risk_budget": {"used_pct_nav": risk, "cap_pct_nav": cfg["risk_budget_pct"]},
        "actions": actions or ["no constraints binding; all proposals approved as-is"],
    }


def _conv(p):
    return float(p.get("conviction", p.get("vote_net", 0.5)) or 0.5)


def allocate_with_competition(held, proposals, cfg=CFG, max_positions=None):
    """When the book is (near) full, capital COMPETES. Rank held + new names by conviction and
    fill top-down until the caps bind. A new name only DISPLACES a held one if it out-convicts it
    by ROTATE_EDGE (anti-churn); held names that fall below the capacity frontier are ROTATED OUT
    (handed to the Position Manager to close via HITL); new names that can't fit are WAITLISTED."""
    cand = []
    for origin, group in (("held", held), ("new", proposals)):
        for p in group:
            size = float(p["size_pct_nav"])
            cand.append({
                "ticker": p["ticker"], "side": p["side"], "origin": origin,
                "sector": p.get("sector", "unknown"), "cluster": p.get("cluster"),
                "conviction": round(_conv(p), 3), "desired": min(size, cfg["max_per_name_pct"]),
                "mll_frac": p.get("max_loss_pct_nav", 0) / max(size, 1e-9),
            })
    # rank by conviction; on a tie keep HELD ahead of NEW; give a held name a small anti-churn edge
    cand.sort(key=lambda c: (c["conviction"] + (ROTATE_EDGE if c["origin"] == "held" else 0.0),
                             c["origin"] == "held"), reverse=True)

    gross_left = cfg["max_gross_pct"]; risk_left = cfg["risk_budget_pct"]
    sector_left = defaultdict(lambda: cfg["max_per_sector_pct"])
    cluster_left = defaultdict(lambda: cfg["max_per_cluster_pct"])
    book, rotate_out, waitlist, actions = [], [], [], []

    for c in cand:
        cap_risk = risk_left / c["mll_frac"] if c["mll_frac"] > 1e-9 else float("inf")
        cap_clus = cluster_left[c["cluster"]] if c["cluster"] else float("inf")
        fit = round(max(0.0, min(c["desired"], gross_left, sector_left[c["sector"]], cap_clus, cap_risk)), 3)
        full = max_positions is not None and len(book) >= max_positions   # sleeve position-count cap
        if fit >= MIN_TICKET_PCT and not full:
            gross_left = round(gross_left - fit, 3); risk_left = round(risk_left - fit * c["mll_frac"], 3)
            sector_left[c["sector"]] = round(sector_left[c["sector"]] - fit, 3)
            if c["cluster"]:
                cluster_left[c["cluster"]] = round(cluster_left[c["cluster"]] - fit, 3)
            trimmed = fit < c["desired"] - 1e-9
            book.append({"ticker": c["ticker"], "side": c["side"], "sector": c["sector"],
                         "cluster": c["cluster"], "conviction": c["conviction"],
                         "approved_pct": fit, "origin": c["origin"], "trimmed": trimmed})
            if trimmed:
                actions.append(f"{c['ticker']} ({c['origin']}, conv {c['conviction']}) trimmed to {fit}% to fit caps")
        else:
            if c["origin"] == "held":
                rotate_out.append({"ticker": c["ticker"], "side": c["side"], "conviction": c["conviction"],
                                   "reason": "below the capacity frontier — outranked; close to free budget"})
                actions.append(f"ROTATE OUT {c['ticker']} (held, conv {c['conviction']}) -> Position Manager close")
            else:
                why = "position cap reached — parked" if full else "no capacity — parked; admit when room opens"
                waitlist.append({"ticker": c["ticker"], "side": c["side"], "conviction": c["conviction"], "reason": why})
                actions.append(f"WAITLIST {c['ticker']} (new, conv {c['conviction']}) — " + ("book at max_positions" if full else "no room at current caps"))

    gross = round(sum(p["approved_pct"] for p in book), 2)
    net = round(sum(p["approved_pct"] * (1 if p["side"] == "long" else -1) for p in book), 2)
    if not actions:
        actions = ["all names fit within caps; no competition needed"]
    return {
        "agent": "portfolio-cio", "mode": "capacity-competition", "as_of": now_iso(),
        "constraints": cfg, "book": sorted(book, key=lambda p: p["conviction"], reverse=True),
        "rotate_out": rotate_out, "waitlist": waitlist,
        "exposures": {"gross_pct": gross, "net_pct": net,
                      "gross_cap": cfg["max_gross_pct"], "capacity_used_pct": round(100 * gross / cfg["max_gross_pct"], 1)},
        "actions": actions,
    }


def main():
    ap = argparse.ArgumentParser(description="Portfolio-level CIO over per-name proposals.")
    ap.add_argument("--book", default=str(OUT / "sample_book.json"),
                    help="single book -> pro-rata fit mode (original)")
    ap.add_argument("--held", help="current open book (JSON) -> capacity-competition mode")
    ap.add_argument("--new", help="new candidate proposals (JSON) for competition mode")
    ap.add_argument("--sleeve", help="book/sleeve name (e.g. levered_etf) -> its OWN caps + max_positions + universe")
    args = ap.parse_args()

    if args.held or args.new:
        held = json.loads(Path(args.held).read_text()) if args.held else []
        proposals = json.loads(Path(args.new).read_text()) if args.new else []
        cfg, maxp, tag, universe = CFG, None, "core", None
        if args.sleeve:
            bk = load_book(args.sleeve); cfg, maxp, tag, universe = bk["cfg"], bk["max_positions"], bk["tag"], bk["universe"]
            if universe:                                     # keep the sleeve to its own names only
                u = set(universe)
                held = [p for p in held if p["ticker"] in u]
                proposals = [p for p in proposals if p["ticker"] in u]
        port = allocate_with_competition(held, proposals, cfg=cfg, max_positions=maxp)
        port["book_tag"] = tag; port["max_positions"] = maxp
        out_name = f"portfolio_decision_{tag}.json" if args.sleeve else "portfolio_decision.json"
        atomic_write_json(OUT / out_name, port)
        ex = port["exposures"]
        print(f"[cio] {tag} | capacity-competition | {len(port['book'])}"
              f"{('/'+str(maxp)) if maxp else ''} in book | gross {ex['gross_pct']}%/{ex['gross_cap']}% | "
              f"{len(port['rotate_out'])} rotate-out, {len(port['waitlist'])} waitlisted -> portfolio/{out_name}")
        for a in port["actions"]:
            print("   -", a)
        return 0

    proposals = json.loads(Path(args.book).read_text())
    port = build_portfolio(proposals)
    atomic_write_json(OUT / "portfolio_decision.json", port)
    ex = port["exposures"]
    print(f"[cio] {len(port['book'])} names | gross {ex['gross_pct']}% net {ex['net_pct']}% "
          f"risk {port['risk_budget']['used_pct_nav']}%/{CFG['risk_budget_pct']}% "
          f"-> portfolio/portfolio_decision.json")
    for a in port["actions"]:
        print("   -", a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
