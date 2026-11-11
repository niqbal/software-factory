"""The shared scheduler: modes, concurrency cap, usage-limit pause and auto-resume."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from asf import limits, scheduler as sched_mod
from asf.scheduler import Scheduler


class FakeStore:
    def __init__(self, jobs):
        self._jobs = {j["id"]: j for j in jobs}

    def jobs(self, states=None):
        return [j for j in self._jobs.values() if states is None or j["state"] in states]

    def job(self, i):
        return self._jobs[i]


class FakeFactory:
    def __init__(self, jobs, models=None):
        self.store = FakeStore(jobs)
        self.retried = []
        m = models or {"provider": "claude_code"}
        self.cfg = SimpleNamespace(get=lambda k, d=None: m if k == "models" else d)

    def paused(self):
        return None

    def poll(self):
        return []

    def retry(self, job_id):
        self.retried.append(job_id)
        self.store.job(job_id)["state"] = "implementing"


def project(key, prio, jobs, models=None):
    p = SimpleNamespace(key=key, priority=prio, archived=False, available=True, hidden=False)
    c = SimpleNamespace(f=FakeFactory(jobs, models), busy=set(), current=None)
    return p, c


def job(i, state="queued", created=0):
    return {"id": i, "state": state, "created": created, "data": {}}


@pytest.fixture
def portfolio(tmp_path, monkeypatch):
    monkeypatch.setenv("ASF_HOME", str(tmp_path))
    a = project("a", 1, [job("a1", created=1), job("a2", "reviewing", created=2)])
    b = project("b", 2, [job("b1", created=0)])
    pf = SimpleNamespace(projects=lambda: [a[0], b[0]], console=lambda k: {"a": a[1], "b": b[1]}[k])
    return pf


def picked(s, n):
    return [j for _, _, j in s.pick(n)]


def test_priority_mode_drains_the_top_project_first(portfolio):
    sched_mod.save_settings(mode="priority")
    s = Scheduler(portfolio)
    assert picked(s, 3) == ["a2", "a1"]          # furthest-along first; b waits although a slot is free


def test_combined_mode_interleaves(portfolio):
    sched_mod.save_settings(mode="combined")
    assert set(picked(Scheduler(portfolio), 2)) == {"a2", "b1"}


def test_separate_mode_one_runner_per_project(portfolio):
    sched_mod.save_settings(mode="separate")
    s = Scheduler(portfolio)
    assert picked(s, 3) == ["a2", "b1"]
    s.running["x"] = ("a", "a2")                  # project a is busy: only b may start
    assert picked(s, 3) == ["b1"]


def test_settings_validated_and_persisted(portfolio):
    with pytest.raises(ValueError):
        sched_mod.save_settings(mode="random")
    assert sched_mod.save_settings(concurrency=50)["concurrency"] == 8
    assert sched_mod.load_settings()["concurrency"] == 8


def test_claude_limit_blocks_claude_only_projects_until_reset(portfolio):
    sched_mod.save_settings(mode="priority", auto_resume=True)
    s = Scheduler(portfolio)
    c = portfolio.console("b")
    c.f.store.job("b1")["state"] = "failed"
    s._hit_limit("b", "b1", "reviewing: claude -p error: You've hit your 5-hour limit · resets 1pm (Asia/Karachi)")
    assert limits.limited_until("claude") is not None
    assert s.limit_jobs == [("b", "b1")] and not c.f.retried      # claude-only project waits
    assert picked(s, 3) == []                                       # a and b are both claude-only
    limits.record_limit("claude", datetime.now().astimezone() - timedelta(seconds=1))
    s._resume_limited()
    assert c.f.retried == ["b1"] and s.limit_jobs == []


def test_pooled_project_moves_to_the_other_backend_at_once(tmp_path, monkeypatch):
    monkeypatch.setenv("ASF_HOME", str(tmp_path))
    pool = {"provider": "pool", "backends": {"claude": {}, "cursor": {}}}
    a = project("a", 1, [job("a1", "failed")], models=pool)
    pf = SimpleNamespace(projects=lambda: [a[0]], console=lambda k: a[1])
    sched_mod.save_settings(auto_resume=True)
    s = Scheduler(pf)
    s._hit_limit("a", "a1", "implementing: [backend:claude] claude -p error: hit your 5-hour limit · resets 1pm")
    assert a[1].f.retried == ["a1"]                                  # cursor is free: retried immediately
    assert limits.backend_of_error("[backend:cursor] cursor-agent error: x") == "cursor"


def test_limit_detection():
    assert limits.is_limit_error("claude -p error: You've hit your 5-hour limit · resets 1pm (Asia/Karachi)")
    assert not limits.is_limit_error("gates failed: diff_size")
