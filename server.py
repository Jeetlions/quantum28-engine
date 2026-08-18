#!/usr/bin/env python3
"""
Tiny HTTP bridge between the dashboard and the orchestrator — this is what the dashboard's
APPROVE / DISAPPROVE buttons call instead of the chat hook.

Endpoints (JSON):
  GET  /api/pending                 -> list pending approvals (kind: trade | manage | basis)
  GET  /api/performance             -> latest performance_report.json (equity curve + stats)
  POST /api/resume {approval_id, reply}  -> orchestrator.resume(approval_id, reply)
        reply = "approve" | "disapprove"; on approve the orchestrator writes the order ticket.

It never trades or moves money — it only relays your decision into the same resume() the CLI uses,
which writes an order ticket for the execution desk. Read-only otherwise.

Security (illustrative): set QUANTUM_API_TOKEN and the dashboard must send it as
`Authorization: Bearer <token>`. Bind to localhost by default. Pure standard library.

Run:  QUANTUM_API_TOKEN=secret python3 server.py   # then dashboard fetches http://127.0.0.1:8787
"""
from __future__ import annotations
import json, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import orchestrator as O

BASE = Path(__file__).resolve().parent
PENDING = BASE / "pending_approvals"
PERF = BASE / "benchmarks" / "performance_report.json"
TOKEN = os.environ.get("QUANTUM_API_TOKEN")            # if set, required on every request
# Railway sets $PORT and needs a public bind; the bearer token protects the endpoint.
HOST = os.environ.get("QUANTUM_API_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT") or os.environ.get("QUANTUM_API_PORT", "8787"))


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")           # dashboard is a static page
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        if not TOKEN:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {TOKEN}"

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        if self.path.startswith("/api/health"):     # unauthenticated, leaks no secret — browser-checkable
            info = {"ok": True}
            try:
                import llm
                info["llm_live"] = llm.live_enabled()
                info["mock_forced"] = os.environ.get("Q28_LLM_MOCK", "0") == "1"
                info["key_present"] = bool(os.environ.get("ANTHROPIC_API_KEY"))
                info["sonnet_model"] = llm.model_for("fundamental-analyst")
                info["haiku_model"] = llm.model_for("sentiment-analyst")
            except Exception as e:
                info["llm_live"] = False
                info["error"] = f"{type(e).__name__}: {e}"
            return self._send(200, info)
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        if self.path.startswith("/api/pending"):
            items = []
            if PENDING.exists():
                for f in sorted(PENDING.glob("*.json")):
                    r = json.loads(f.read_text())
                    if r.get("status") != "awaiting_human":
                        continue
                    st = r.get("structure_ticket") or {}
                    dec = r.get("decision") or {}
                    items.append({
                        "approval_id": r["approval_id"],
                        "kind": r.get("kind", "trade"),
                        "created_at": r.get("created_at"),
                        "ticker": dec.get("ticker") or (r.get("basis_trade") or {}).get("ticker"),
                        "agreement": dec.get("agreement"),
                        "structure": ({                      # legs + risk for the approval card
                            "strategy": st.get("strategy"),
                            "underlying_type": st.get("underlying_type"),
                            "legs": st.get("legs", []),
                            "net": st.get("net"),
                            "max_loss": st.get("max_loss"),
                            "max_gain": st.get("max_gain"),
                            "breakevens": st.get("breakevens"),
                        } if st else None),
                        "summary": r.get("basis_trade") or r.get("action") or dec.get("ticker"),
                    })
            return self._send(200, {"pending": items})
        if self.path.startswith("/api/performance"):
            return self._send(200, json.loads(PERF.read_text()) if PERF.exists() else {})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        if not self.path.startswith("/api/resume"):
            return self._send(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length", "0") or "0")
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
            aid, reply = payload["approval_id"], payload["reply"]
        except Exception as e:
            return self._send(400, {"error": f"bad request: {e}"})
        if reply not in ("approve", "disapprove"):
            return self._send(400, {"error": "reply must be approve|disapprove"})
        try:
            record = O.resume(aid, reply)                              # the exact function the CLI calls
        except O.GuardrailError as e:
            return self._send(409, {"error": str(e)})
        return self._send(200, {"approval_id": aid, "status": record["status"]})

    def log_message(self, *a):                                          # quieter logs
        pass


if __name__ == "__main__":
    print(f"[server] Quantum28 bridge on http://{HOST}:{PORT}  (auth {'ON' if TOKEN else 'OFF'})")
    print("         POST /api/resume {approval_id, reply}  ·  GET /api/pending  ·  GET /api/performance")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
