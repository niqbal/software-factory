"""The local console: read views, token-guarded actions, backlog dependencies."""

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from test_pipeline import factory, item, provider

from asf.ui import Console, make_handler


def start(f):
    console = Console(f)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(console))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return console, server, f"http://127.0.0.1:{server.server_address[1]}"


def get(url):
    return json.loads(urllib.request.urlopen(url).read())


def post(url, token=None, body=None):
    req = urllib.request.Request(url, data=json.dumps(body or {}).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **({"X-ASF-Token": token} if token else {})})
    return json.loads(urllib.request.urlopen(req).read())


def test_console_views_and_merge(invoice_repo, state, store):
    f = factory(invoice_repo, state, store, provider(), **{"observe.post_merge": ["true"]})
    job_id, _ = f.submit(item())
    assert f.run_job(job_id) == "awaiting_review"
    console, server, base = start(f)
    try:
        page = urllib.request.urlopen(base + "/").read().decode()
        assert console.token in page and "Factory console" in page
        st = get(base + "/api/state")
        assert [j["state"] for j in st["jobs"]] == ["awaiting_review"]
        detail = get(f"{base}/api/jobs/{job_id}")
        assert "merge" in detail["actions"] and "invoice/totals.py" in detail["diff"]["stat"]
        try:
            post(f"{base}/api/jobs/{job_id}/merge")  # no token: refused
            raise AssertionError("POST without token was accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 403
        assert "merging" in post(f"{base}/api/jobs/{job_id}/merge", console.token)["message"]
        for _ in range(100):
            if store.job(job_id)["state"] == "done":
                break
            time.sleep(0.1)
        assert store.job(job_id)["state"] == "done"
        assert get(base + "/api/state")["metrics"]["counts"]["shipped"] == 1
    finally:
        server.shutdown()


def test_backlog_dependencies(invoice_repo, state, store, tmp_path):
    backlog = invoice_repo / "factory" / "backlog"
    backlog.mkdir(parents=True)
    (invoice_repo / "factory" / "done").mkdir()
    (invoice_repo / "factory" / "done" / "WI-001.md").write_text("done\n")
    (backlog / "WI-002.md").write_text("---\ntitle: Two\ndepends_on: [WI-001]\n---\nbody\n")
    (backlog / "WI-003.md").write_text("---\ntitle: Three\ndepends_on: [WI-001, WI-002]\n---\nbody\n")
    f = factory(invoice_repo, state, store, provider(),
                **{"intake.local.backlog": "factory/backlog", "intake.local.done": "factory/done",
                   "intake.local.path": "factory/ready"})
    console, server, base = start(f)
    try:
        bl = {b["id"]: b for b in get(base + "/api/state")["backlog"]}
        assert bl["WI-002"]["ready"] and not bl["WI-003"]["ready"] and bl["WI-003"]["blocked_by"] == ["WI-002"]
        msg = post(f"{base}/api/backlog/WI-002/queue", console.token)["message"]
        assert "queued WI-002" in msg and (invoice_repo / "factory" / "ready" / "WI-002.md").exists()
    finally:
        server.shutdown()
