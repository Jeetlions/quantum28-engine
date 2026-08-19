#!/usr/bin/env python3
"""
Quantum28 runtime — ONE always-on process for Railway (or a VPS): the HTTP bridge (server.py)
plus an internal ET-aware scheduler that fires the automations. One service, one volume — which
is what Railway needs, since a volume attaches to a single service.

On boot it points the durable state dirs at the mounted volume (RAILWAY_VOLUME_MOUNT_PATH or
STATE_DIR), starts the dashboard bridge in a thread, then runs the schedule from automations.yaml.

Observe/paper: nothing here executes a trade; every proposal still waits for your tap.
Start command:  python3 run.py
"""
from __future__ import annotations
import os, shutil, subprocess, sys, threading, time
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = None

BASE = Path(__file__).resolve().parent
STATE_DIRS = ["shared", "pending_approvals", "orders", "positions", "portfolio", "basis", "benchmarks"]


def mount_state():
    """Put durable state on the volume; copy any committed seeds over once, then symlink."""
    vol = os.environ.get("STATE_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if not vol:
        print("[nexus] no volume ($STATE_DIR / $RAILWAY_VOLUME_MOUNT_PATH) — using local dirs")
        return
    for d in STATE_DIRS:
        tgt = Path(vol) / d; tgt.mkdir(parents=True, exist_ok=True)
        link = BASE / d
        if link.is_symlink():
            continue
        if link.exists():                              # committed seeds (sample views, seed logs)
            for f in link.iterdir():
                dest = tgt / f.name
                if not dest.exists():
                    (shutil.copytree if f.is_dir() else shutil.copy2)(f, dest)
            shutil.rmtree(link)
        link.symlink_to(tgt)
    print(f"[nexus] state on volume {vol}")


def start_bridge():
    import server                                       # server.py Handler; binds 0.0.0.0:$PORT
    port = int(os.environ.get("PORT") or os.environ.get("QUANTUM_API_PORT", "8787"))
    print(f"[nexus] bridge on 0.0.0.0:{port} (auth {'ON' if os.environ.get('QUANTUM_API_TOKEN') else 'OFF'})")
    ThreadingHTTPServer(("0.0.0.0", port), server.Handler).serve_forever()


def now_et():
    return datetime.now(ET) if ET else datetime.utcnow()


def due(dt):
    """Return the list of commands due at this ET minute (mirrors automations.yaml)."""
    wd, h, m = dt.weekday(), dt.hour, dt.minute        # wd 0=Mon..4=Fri
    biz = wd < 5
    jobs = []
    zero_dte = [["orchestrator.py", "run", "--ticker", t, "--tenor", "0DTE"]
                for t in ("SPX", "NDX", "RUT")]                                      # 3 indices
    if biz and h == 7 and m == 0:            jobs.append(["brief.py"])
    if biz and h == 9 and m == 35:           jobs += zero_dte                       # 0DTE breakouts
    if biz and h == 9 and m == 45:           jobs.append(["scan_watchlist.py", "--bucket", "monitor"])
    if biz and h == 12 and m == 30:          jobs += zero_dte                       # 0DTE flies (midday)
    if biz and 10 <= h < 16 and not (h == 15 and m > 30) and m in (0, 30):
        jobs.append(["decay_signals.py"])                                           # INTRADAY decay
    if biz and h in (10, 14) and m == 0:     jobs.append(["basis.py"])              # basis (monitor-only)
    if biz and h == 15 and m == 45:          jobs.append(["scan_watchlist.py", "--bucket", "monitor"])
    if biz and h == 16 and m == 30:          jobs.append(["performance.py"])
    if biz and h == 17 and m == 0:           jobs.append(["scan_watchlist.py", "--bucket", "equities"])  # daily equity vote
    if wd == 4 and h == 18 and m == 0:       jobs.append(["benchmarks/benchmark.py"])   # weekly re-weight
    if wd == 4 and h == 18 and m == 30:      jobs.append(["calibration.py"])            # weekly re-calibrate
    return jobs


def run_job(cmd):
    print(f"[nexus] {now_et():%Y-%m-%d %H:%M ET}  ->  {' '.join(cmd)}")
    try:
        subprocess.Popen([sys.executable] + cmd, cwd=str(BASE))
    except Exception as e:
        print("[nexus] job failed:", e)


def main():
    mount_state()
    threading.Thread(target=start_bridge, daemon=True).start()
    print("[nexus] scheduler up (US/Eastern). OBSERVE/PAPER — every trade waits for your tap.")
    last = None
    while True:
        dt = now_et(); key = (dt.hour, dt.minute)
        if key != last:
            for cmd in due(dt):
                run_job(cmd)
            last = key
        time.sleep(15)


if __name__ == "__main__":
    raise SystemExit(main())
