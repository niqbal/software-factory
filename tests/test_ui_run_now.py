"""Regression tests for Run now behavior while a job is actively running."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from test_pipeline import factory, item, provider

from asf.projects import Project
from asf.ui import Console, Portfolio, make_handler

KEY = "invoice-abc123"


def start(f):
    portfolio = Portfolio()
    console = Console(f)
    console.token = portfolio.token
    console.project = Project(key=KEY, state_dir=f.state_dir, repo=f.repo, name="invoice")
    portfolio.consoles[KEY] = console
    portfolio.projects = lambda: [console.project]
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(portfolio))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return console, server, f"http://127.0.0.1:{server.server_address[1]}"


def get(url):
    return json.loads(urllib.request.urlopen(url).read())


def post(url, token=None, body=None):
    req = urllib.request.Request(url, data=json.dumps(body or {}).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          **({"X-ASF-Token": token} if token else {})})
    return json.loads(urllib.request.urlopen(req).read())


def test_run_now_hidden_and_guarded_while_job_is_running(invoice_repo, state, store):
    f = factory(invoice_repo, state, store, provider())
    job_id, _ = f.submit(item())
    console, server, base = start(f)
    try:
        detail = get(f"{base}/api/p/{KEY}/jobs/{job_id}")
        assert "run" in detail["actions"]

        with console.lock:
            console.busy.add(job_id)

        running_detail = get(f"{base}/api/p/{KEY}/jobs/{job_id}")
        assert "run" not in running_detail["actions"]
        assert running_detail["running"] is True

        state_view = get(f"{base}/api/p/{KEY}/state")
        [job] = [j for j in state_view["jobs"] if j["id"] == job_id]
        assert job["running"] is True

        try:
            post(f"{base}/api/p/{KEY}/jobs/{job_id}/run", console.token)
            raise AssertionError("Run was accepted while job was already running")
        except urllib.error.HTTPError as e:
            assert e.code == 400
            assert "already running" in json.loads(e.read())["error"]

        with console.lock:
            console.busy.clear()

        idle_detail = get(f"{base}/api/p/{KEY}/jobs/{job_id}")
        assert "run" in idle_detail["actions"]
        assert post(f"{base}/api/p/{KEY}/jobs/{job_id}/run", console.token)["message"] == "running"
    finally:
        server.shutdown()
