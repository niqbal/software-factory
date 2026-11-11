"""Gateway: webhook intake with signature checks, a job API, and an optional worker.

POST /webhooks/<adapter>   verify signature, normalize, enqueue jobs
GET  /healthz
GET  /jobs                 job list (requires ASF_ADMIN_TOKEN bearer)
POST /jobs/<id>/approve    record an approval (requires ASF_ADMIN_TOKEN bearer)
POST /jobs/<id>/reject
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .orchestrator import Factory


def make_handler(factory: Factory, admin_token: str | None):
    adapters = factory.intake_adapters()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: Any) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            return bool(admin_token) and self.headers.get("Authorization", "") == f"Bearer {admin_token}"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter logs
            factory.echo(f"gateway {self.address_string()} {fmt % args}")

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                return self._send(200, {"ok": True, "paused": factory.paused()})
            if self.path == "/jobs":
                if not self._authorized():
                    return self._send(401, {"error": "unauthorized"})
                return self._send(200, [{k: j[k] for k in ("id", "state", "tier")} for j in factory.store.jobs()])
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            headers = {k: v for k, v in self.headers.items()}
            parts = self.path.strip("/").split("/")
            if len(parts) == 2 and parts[0] == "webhooks":
                adapter = adapters.get(parts[1])
                if adapter is None:
                    return self._send(404, {"error": f"intake '{parts[1]}' is not enabled"})
                if parts[1] == "slack" and body.startswith(b"{"):
                    payload = json.loads(body)
                    if payload.get("type") == "url_verification" and adapter.verify_webhook(headers, body):
                        return self._send(200, {"challenge": payload.get("challenge")})
                if not adapter.verify_webhook(headers, body):
                    return self._send(401, {"error": "bad signature"})
                jobs = [factory.submit(item)[0] for item in adapter.normalize_webhook(headers, body)]
                return self._send(202, {"jobs": jobs})
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] in ("approve", "reject"):
                if not self._authorized():
                    return self._send(401, {"error": "unauthorized"})
                payload = json.loads(body or b"{}")
                factory.store.approve(parts[1], payload.get("by", "api"), parts[2], payload.get("reason", ""))
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})

    return Handler


def worker_loop(factory: Factory, stop: threading.Event, interval: float = 5.0) -> None:
    while not stop.is_set():
        for job in factory.store.jobs(["queued", "triaged", "implementing", "testing", "verifying",
                                       "reviewing", "committing", "delivering", "awaiting_approval",
                                       "awaiting_review", "merging", "merged",
                                       "observing"]):
            if stop.is_set() or factory.paused():
                break
            factory.run_job(job["id"])
        stop.wait(interval)


def serve(factory: Factory, port: int, admin_token: str | None, worker: bool) -> None:
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(factory, admin_token))
    stop = threading.Event()
    if worker:
        threading.Thread(target=worker_loop, args=(factory, stop), daemon=True).start()
    factory.echo(f"gateway listening on :{port} (worker {'on' if worker else 'off'})")
    try:
        server.serve_forever()
    finally:
        stop.set()
        time.sleep(0.1)
