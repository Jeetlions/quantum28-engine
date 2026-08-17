# Quantum28 — Deployment Runbook

Get the fund running in **observe / paper mode** first. Nothing here executes trades: every
proposal waits for your Telegram/dashboard tap, and the agent only writes order tickets.

## 0. What it needs (both hosts)
- An **always-on worker** (the Hermes agent / messaging gateway) — not a cron-exit job.
- A **persistent volume** for durable state: `shared/`, `pending_approvals/`, `orders/`,
  `positions/`, `benchmarks/`. Losing these breaks suspend/resume-survives-restart.
- **Cron/scheduled runs** for `automations.yaml` (morning brief, 0DTE passes, **intraday decay
  scan**, basis scan, performance, weekly re-weight/re-calibrate).
- **Secrets as env vars** (copy `.env.example` → set real values). Never commit them.
- **No separate database required**: file-based state on the volume. For the signal rolling
  histories (Smart-PCR Z, call-convexity Z, IV rank, VIX Z, decile calibration) use **SQLite or
  Parquet on the same volume**. A managed Postgres is optional (Railway add-on) if the dashboard
  and multiple processes need concurrent reads.

## 1a. Railway (least ops — recommended to start)
1. Create a Railway project; **Deploy from GitHub** (this repo).
2. Add a **Volume**, mount at the app dir (e.g. `/app/state`) — point the framework's dirs there.
3. Set **Variables** from `.env.example` (models, TASTY_*, IB_*, FRED/EIA/NOAA/FINNHUB keys,
   TELEGRAM_*, QUANTUM_API_TOKEN, SSF_GO_LIVE).
4. Service A — **worker** (always-on): runs the Hermes agent / gateway.
5. Service B..N — **cron services**, one per `automations.yaml` entry (Railway cron min = 5 min;
   the intraday decay scan `*/30 10-15 * * 1-5` fits). Or run a single scheduler process.
6. Expose **`server.py`** (the dashboard bridge) as an HTTP service; set `QUANTUM_API_TOKEN`;
   Railway gives you HTTPS. Point the dashboard's `API_BASE` at it.
7. Railway **auto-restarts** on crash/redeploy — the volume preserves pending approvals.

## 1b. Hostinger VPS (full control)
1. Provision a **KVM VPS** (Ubuntu), `apt install python3-pip`, clone the repo, `pip install` deps.
2. **systemd** unit keeps the worker alive + auto-starts on reboot:
   `systemctl enable --now quantum28-worker`.
3. **cron** for the schedule (`crontab -e`) mirroring `automations.yaml` (use ET; set `TZ`).
4. **nginx** reverse proxy + TLS (certbot) in front of `server.py`; keep it on localhost + token.
5. State lives on the VPS disk (it's persistent); back it up (weekly snapshot).
6. Do **not** use shared hosting — it kills long-running processes.

## 2. Data setup (see data_sources.yaml for the full map)
- **Markets** — Tasty (primary options greeks) + IB (equities/futures: VIX, NG, CL curves, and
  SSF once live). You already have paid IB + Tasty, so **intraday** options data is available —
  which the decay sleeve relies on (a VIX spike to 55 that closes near 30 is gone by EOD).
- **Macro (free keys)** — FRED (series list in data_sources.yaml), EIA (nat-gas storage Thu,
  crude inventory Wed), NOAA CPC (6-15 day HDD/CDD).
- **Events/news (free tier)** — Finnhub earnings calendar (drives the IV cycle), IPO calendar,
  company news; SEC EDGAR for filings + Form 4.
- Only real spend: your **OPRA real-time entitlement** on Tasty/IB (non-professional).

## 3. Cadence (automations.yaml)
Morning brief 07:00 · 0DTE breakouts 09:35 · **decay scan every 30 min 10:00–15:30** · basis scan
10:00 & 14:00 (**monitor-only until `SSF_GO_LIVE`**) · 0DTE flies 12:30 · performance 16:30 ·
Fri 18:00 re-weight · Fri 18:30 re-calibrate.

## 4. Go-live checklist
- [ ] Volume mounted; state dirs writable and surviving a restart.
- [ ] All env keys set; `python3 datafeed.py --symbol SPX` shows a live backend (not demo).
- [ ] Telegram bot reachable; `server.py` behind token + HTTPS; dashboard `API_BASE` set.
- [ ] `SSF_GO_LIVE` correct (basis stays monitor-only until then).
- [ ] Running in **observe/paper**; confirm order tickets are written but nothing auto-executes.
- [ ] Backups of the state volume scheduled.
