from __future__ import annotations

from argparse import Namespace

from asf.cli import App, cmd_intake
from asf.models import WorkItem

from conftest import invoice_config, load_cfg


class StubAdapter:
    def __init__(self, items=None, error: Exception | None = None):
        self._items = items or []
        self._error = error

    def poll(self):
        if self._error:
            raise self._error
        return list(self._items)

    def fetch(self, external_id: str):
        for item in self._items:
            if item.external_id == external_id:
                return item
        raise KeyError(external_id)


class FactoryStub:
    def __init__(self, adapters: dict[str, StubAdapter]):
        self._adapters = adapters
        self.submitted: list[tuple[str, str]] = []
        self._job_state: dict[str, str] = {}
        self._n = 0

    def intake_adapters(self):
        return dict(self._adapters)

    @property
    def store(self):
        return self

    def job(self, job_id: str):
        return {"state": self._job_state.get(job_id, "queued")}

    def submit(self, item: WorkItem, rerun: bool = False):
        key = (item.source_kind, item.external_id)
        if key in self.submitted:
            return "JOB-EXISTING", False
        self.submitted.append(key)
        self._n += 1
        job_id = f"JOB-{self._n}"
        self._job_state[job_id] = "queued"
        return job_id, True

    def poll(self, source: str | None = None, ids: list[str] | None = None, rerun: bool = False):
        _ = (source, ids, rerun)
        return []


def _app_with_stub(invoice_repo, state, adapters, source=None, ids=None):
    cfg = load_cfg(invoice_config(**{"intake.local.enabled": False, "intake.github_issues": {"enabled": True}}))
    args = Namespace(
        repo=str(invoice_repo),
        config=None,
        state=str(state),
        env_file=None,
        set=[],
        source=source,
        id=ids,
        rerun=False,
    )
    app = App(args)
    app._cfg = cfg
    app._factory = FactoryStub(adapters)
    return app


def test_cmd_intake_lists_external_issue_metadata(invoice_repo, state, capsys):
    items = [
        WorkItem("github_issues", "7", "Show issues in console", url="https://github.com/o/r/issues/7"),
        WorkItem("github_issues", "9", "Handle failures", url="https://github.com/o/r/issues/9"),
    ]
    app = _app_with_stub(invoice_repo, state, {"github_issues": StubAdapter(items=items)})

    rc = cmd_intake(app)

    out = capsys.readouterr().out
    assert rc == 0
    assert "[github_issues] 2 issue(s)" in out
    assert "- [github_issues] #7 Show issues in console (https://github.com/o/r/issues/7)" in out
    assert "- [github_issues] #9 Handle failures (https://github.com/o/r/issues/9)" in out
    assert "2 new job(s): JOB-1, JOB-2" in out


def test_cmd_intake_external_failure_is_clear_and_non_crashing(invoice_repo, state, capsys):
    app = _app_with_stub(
        invoice_repo,
        state,
        {"github_issues": StubAdapter(error=RuntimeError("HTTP 401: Bad credentials"))},
    )

    rc = cmd_intake(app)

    out = capsys.readouterr().out
    assert rc == 1
    assert "intake error [github_issues]: HTTP 401: Bad credentials" in out
    assert "0 new job(s): -" in out


def test_cmd_intake_external_empty_state(invoice_repo, state, capsys):
    app = _app_with_stub(invoice_repo, state, {"github_issues": StubAdapter(items=[])})

    rc = cmd_intake(app)

    out = capsys.readouterr().out
    assert rc == 0
    assert "[github_issues] no issues available" in out
    assert "no external issues found" in out
    assert "0 new job(s): -" in out


def test_cmd_intake_local_only_behavior_is_unchanged(invoice_repo, state, capsys):
    inbox = state / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "WI-1.md").write_text("---\ntitle: Local task\n---\nBody\n")

    cfg = load_cfg(invoice_config())
    args = Namespace(
        repo=str(invoice_repo),
        config=None,
        state=str(state),
        env_file=None,
        set=[],
        source=None,
        id=None,
        rerun=False,
    )
    app = App(args)
    app._cfg = cfg

    rc = cmd_intake(app)

    out = capsys.readouterr().out
    assert rc == 0
    assert "queued JOB-" in out
    assert "[local]" not in out
    assert "1 new job(s): JOB-" in out
