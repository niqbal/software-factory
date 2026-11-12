"""UI intake endpoint: external issue listing, empty state, and error handling."""

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from asf.models import WorkItem
from asf.projects import Project
from asf.ui import Console, Portfolio, make_handler

KEY = "invoice-abc123"


class StubAdapter:
    def __init__(self, items=None, error: Exception | None = None):
        self._items = items or []
        self._error = error

    def poll(self):
        if self._error:
            raise self._error
        return list(self._items)


class StubStore:
    def __init__(self):
        self._states: dict[str, str] = {}

    def remember(self, job_id: str, state: str = "queued") -> None:
        self._states[job_id] = state

    def job(self, job_id: str):
        return {"state": self._states.get(job_id, "queued")}


class StubFactory:
    def __init__(self, repo, state_dir, adapters, local_jobs=0):
        self.repo = repo
        self.state_dir = state_dir
        self._adapters = dict(adapters)
        self._local_jobs = int(local_jobs)
        self._n = 0
        self.store = StubStore()
        self.echo = lambda msg: None

    def intake_adapters(self):
        return dict(self._adapters)

    def pick_up_replies(self):
        return []

    def poll(self, source=None, ids=None, rerun=False):
        _ = (ids, rerun)
        if source == "local":
            return [f"JOB-L{i + 1}" for i in range(self._local_jobs)]
        return []

    def submit(self, item, rerun=False):
        _ = (item, rerun)
        self._n += 1
        job_id = f"JOB-{self._n}"
        self.store.remember(job_id)
        return job_id, True


def start(factory):
    portfolio = Portfolio()
    console = Console(factory)
    console.token = portfolio.token
    console.project = Project(key=KEY, state_dir=factory.state_dir, repo=factory.repo, name="invoice")
    portfolio.consoles[KEY] = console
    portfolio.projects = lambda: [console.project]
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(portfolio))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return console, server, f"http://127.0.0.1:{server.server_address[1]}"


def post(url, token=None, body=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(body or {}).encode(),
        method="POST",
        headers={"Content-Type": "application/json", **({"X-ASF-Token": token} if token else {})},
    )
    with urllib.request.urlopen(req) as resp:
        return resp.getcode(), json.loads(resp.read())


def test_ui_intake_lists_external_issue_metadata(invoice_repo, state):
    items = [
        WorkItem("github_issues", "7", "Show issues in console", url="https://github.com/o/r/issues/7"),
        WorkItem("github_issues", "9", "Handle failures", url="https://github.com/o/r/issues/9"),
    ]
    f = StubFactory(invoice_repo, state, {"github_issues": StubAdapter(items=items)})
    console, server, base = start(f)
    try:
        code, res = post(f"{base}/api/p/{KEY}/intake", console.token)
        out = res["message"]
        assert code == 200
        assert "[github_issues] 2 issue(s)" in out
        assert "- [github_issues] #7 Show issues in console (https://github.com/o/r/issues/7)" in out
        assert "- [github_issues] #9 Handle failures (https://github.com/o/r/issues/9)" in out
        assert "2 new job(s): JOB-1, JOB-2" in out
        assert res["errors"] == 0
        assert res["new_jobs"] == ["JOB-1", "JOB-2"]
    finally:
        server.shutdown()


def test_ui_intake_external_failure_is_clear_and_non_crashing(invoice_repo, state):
    f = StubFactory(
        invoice_repo,
        state,
        {"github_issues": StubAdapter(error=RuntimeError("HTTP 401: Bad credentials"))},
    )
    console, server, base = start(f)
    try:
        code, res = post(f"{base}/api/p/{KEY}/intake", console.token)
        out = res["message"]
        assert code == 200
        assert "intake error [github_issues]: HTTP 401: Bad credentials" in out
        assert "0 new job(s): -" in out
        assert res["errors"] == 1
        assert res["new_jobs"] == []
    finally:
        server.shutdown()


def test_ui_intake_external_empty_state(invoice_repo, state):
    f = StubFactory(invoice_repo, state, {"github_issues": StubAdapter(items=[])})
    console, server, base = start(f)
    try:
        code, res = post(f"{base}/api/p/{KEY}/intake", console.token)
        out = res["message"]
        assert code == 200
        assert "[github_issues] no issues available" in out
        assert "no external issues found" in out
        assert "0 new job(s): -" in out
        assert res["errors"] == 0
        assert res["new_jobs"] == []
    finally:
        server.shutdown()


def test_ui_intake_local_only_keeps_existing_message(invoice_repo, state):
    f = StubFactory(invoice_repo, state, {"local": StubAdapter(items=[])}, local_jobs=1)
    console, server, base = start(f)
    try:
        code, res = post(f"{base}/api/p/{KEY}/intake", console.token)
        assert code == 200
        assert res["message"] == "1 new job(s)"
        assert res["errors"] == 0
        assert res["new_jobs"] == ["JOB-L1"]
    finally:
        server.shutdown()
