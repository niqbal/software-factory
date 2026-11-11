"""The local console: read views, token-guarded actions, backlog dependencies."""

import json
import threading
import time
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
        st = get(f"{base}/api/p/{KEY}/state")
        assert [j["state"] for j in st["jobs"]] == ["awaiting_review"]
        pf = get(base + "/api/portfolio")
        assert pf["projects"][0]["needs_you"] == 1 and [i["id"] for i in pf["inbox"]] == [job_id]
        assert pf["inbox"][0]["project"] == KEY
        detail = get(f"{base}/api/p/{KEY}/jobs/{job_id}")
        assert "merge" in detail["actions"] and "invoice/totals.py" in detail["diff"]["stat"]
        try:
            post(f"{base}/api/p/{KEY}/jobs/{job_id}/merge")  # no token: refused
            raise AssertionError("POST without token was accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 403
        assert "merging" in post(f"{base}/api/p/{KEY}/jobs/{job_id}/merge", console.token)["message"]
        for _ in range(100):
            if store.job(job_id)["state"] == "done":
                break
            time.sleep(0.1)
        assert store.job(job_id)["state"] == "done"
        assert get(f"{base}/api/p/{KEY}/state")["metrics"]["counts"]["shipped"] == 1
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
        bl = {b["id"]: b for b in get(f"{base}/api/p/{KEY}/state")["backlog"]}
        assert bl["WI-002"]["ready"] and not bl["WI-003"]["ready"] and bl["WI-003"]["blocked_by"] == ["WI-002"]
        msg = post(f"{base}/api/p/{KEY}/backlog/WI-002/queue", console.token)["message"]
        assert "queued WI-002" in msg and (invoice_repo / "factory" / "ready" / "WI-002.md").exists()
    finally:
        server.shutdown()


def test_discovery_and_registry(tmp_path, monkeypatch):
    from asf import projects
    from asf.util import sha
    home = tmp_path / "asf"
    repo = tmp_path / "work" / "demo"
    repo.mkdir(parents=True)
    key = f"demo-{sha(str(repo.resolve()), 6)}"
    (home / key).mkdir(parents=True)
    (home / key / "factory.db").write_text("")
    monkeypatch.setenv("ASF_HOME", str(home))
    monkeypatch.setattr(projects, "SEARCH_ROOTS", [str(tmp_path / "work")])
    [p] = projects.discover()
    assert p.repo == repo.resolve() and (home / key / "project.json").exists()  # recovered, then recorded
    projects.set_entry(key, name="Demo", priority=1, archived=True)
    [p] = projects.discover()
    assert (p.name, p.priority, p.archived) == ("Demo", 1, True)


def test_portfolio_reloads_an_edited_factory_yaml(tmp_path, monkeypatch):
    import os
    from conftest import make_repo
    from asf import cli
    monkeypatch.setenv("ASF_HOME", str(tmp_path / "home"))
    repo = make_repo(tmp_path / "game", {"package.json": json.dumps({"scripts": {"test": "node --test"}})})
    assert cli.main(["--repo", str(repo), "init", "--no-branch"]) == 0
    (repo / "factory.yaml").write_text((next((tmp_path / "home").iterdir()) / "factory.yaml").read_text())
    portfolio = Portfolio()
    [p] = portfolio.projects()
    first = portfolio.console(p.key)
    assert portfolio.console(p.key) is first
    first.busy.add("JOB-1")                       # a job is running: keep the console
    os.utime(repo / "factory.yaml", (1, 1))
    assert portfolio.console(p.key) is first
    first.busy.clear()
    second = portfolio.console(p.key)
    assert second is not first and second.cfg_stamp == 1
