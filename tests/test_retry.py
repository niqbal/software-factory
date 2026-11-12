"""Stopped jobs: automatic retry when no human is needed, and rerun from the item's own source."""

import pytest

from asf.adapters.intake.local_file import LocalFileIntake
from asf.models import WorkItem
from test_pipeline import IMPL, factory, item, provider

UNAVAILABLE = "implementing: [backend:cursor] cursor-agent error (exit 1): Error: [unavailable] HTTP 502"
BLOCKED = {"name": "finish", "input": {"summary": "Stopped early.", "blocked_reason": "stopped before implementing"}}
# The model ends a turn without calling finish, is nudged, and then reports itself blocked.
NUDGED_THEN_BLOCKED = [[{"name": "read_file", "input": {"path": "invoice/totals.py"}}], [], [BLOCKED]]


def events(store, job_id, type_):
    return [e for e in store.events(job_id) if e["type"] == type_]


def test_blocked_only_after_a_nudge_is_retried_and_finishes(invoice_repo, state, store):
    f = factory(invoice_repo, state, store, provider(implementer=[NUDGED_THEN_BLOCKED, IMPL]))
    job_id, _ = f.submit(item())
    assert f.run_job(job_id) == "needs_human"
    job = store.job(job_id)
    assert job["data"]["incomplete"] and f.retry_reason(job).startswith("incomplete")
    assert f.auto_retry(job_id) == "implementing"
    assert f.run_job(job_id) == "awaiting_review"
    [ev] = events(store, job_id, "job.auto_retry")
    assert ev["payload"]["attempt"] == 1 and ev["payload"]["from"] == "needs_human"


def test_a_real_block_waits_for_a_human(invoice_repo, state, store):
    f = factory(invoice_repo, state, store, provider(implementer=[[[BLOCKED]]]))
    job_id, _ = f.submit(item())
    assert f.run_job(job_id) == "needs_human"
    assert f.retry_reason(store.job(job_id)) is None
    assert f.auto_retry(job_id) is None and store.job(job_id)["state"] == "needs_human"


def test_transient_failures_retry_up_to_the_cap_then_wait(invoice_repo, state, store):
    f = factory(invoice_repo, state, store, provider(), **{"limits.auto_retries": 2})
    job_id, _ = f.submit(item())
    for attempt in (1, 2):
        store.update_job(job_id, "failed", error=UNAVAILABLE, failed_state="queued")
        assert f.auto_retry(job_id) == "queued", attempt
    store.update_job(job_id, "failed", error=UNAVAILABLE, failed_state="queued")
    assert f.auto_retry(job_id) is None
    assert store.job(job_id)["state"] == "failed"
    assert len(events(store, job_id, "job.auto_retry")) == 2
    assert len(events(store, job_id, "job.auto_retry_exhausted")) == 1
    f.auto_retry(job_id)  # asking again does not repeat the give-up event
    assert len(events(store, job_id, "job.auto_retry_exhausted")) == 1
    # A human retry starts the count over.
    f.retry(job_id)
    store.update_job(job_id, "failed", error=UNAVAILABLE, failed_state="queued")
    assert f.auto_retry(job_id) == "queued"


@pytest.mark.parametrize("error", ["implementing: KeyError: 'summary'",
                                   "implementing: You've hit your 5-hour limit · resets 1pm"])
def test_bugs_and_usage_limits_are_not_retried_here(invoice_repo, state, store, error):
    f = factory(invoice_repo, state, store, provider())
    job_id, _ = f.submit(item())
    store.update_job(job_id, "failed", error=error, failed_state="queued")
    assert f.auto_retry(job_id) is None and store.job(job_id)["state"] == "failed"


class FakeGitHub:
    kind = "github_issues"

    def __init__(self, title="Add console issue creation (edited)"):
        self.title = title

    def fetch(self, external_id):
        return WorkItem("github_issues", external_id, self.title, body="new body")


def test_rerun_fetches_from_the_items_own_source(invoice_repo, state, store):
    """Regression: rerun asked every intake adapter in turn, and the local inbox (first) raised
    on a GitHub issue number after the old job was already rejected. Nothing replaced it."""
    f = factory(invoice_repo, state, store, provider())
    local = LocalFileIntake({}, f.ctx)
    f.intake_adapters = lambda: {"local": local, "github_issues": FakeGitHub()}
    old, _ = f.submit(WorkItem("github_issues", "8", "Add console issue creation"))
    store.update_job(old, "needs_human", needs_human_reason="implementer blocked")
    new = f.rerun(old, by="owner (ui)")
    assert new != old and store.job(new)["state"] == "queued"
    assert store.job(new)["data"]["rerun_of"] == old
    assert store.job(old)["state"] == "rejected" and store.job(old)["data"]["rerun_as"] == new
    assert events(store, old, "job.rerun")[0]["payload"]["new_job"] == new
    # The new job works from the issue as it reads now.
    assert store.get_work_item(store.job(new)["work_item_id"]).title.endswith("(edited)")


def test_a_failed_rerun_leaves_the_job_and_says_why(invoice_repo, state, store):
    f = factory(invoice_repo, state, store, provider())
    f.intake_adapters = lambda: {"local": LocalFileIntake({}, f.ctx)}
    old, _ = f.submit(WorkItem("github_issues", "8", "Add console issue creation"))
    store.update_job(old, "needs_human", needs_human_reason="implementer blocked")
    with pytest.raises(ValueError, match="not enabled"):
        f.rerun(old)
    assert store.job(old)["state"] == "needs_human"
    assert "not enabled" in events(store, old, "job.rerun_failed")[0]["payload"]["error"]
    assert len(store.jobs()) == 1


def test_rerun_refuses_a_job_still_in_progress(invoice_repo, state, store):
    f = factory(invoice_repo, state, store, provider())
    job_id, _ = f.submit(item())
    with pytest.raises(ValueError, match="queued"):
        f.rerun(job_id)


def _interrupted_after_half_the_work(f):
    """Run the first implementer attempt, then fail as if the backend returned a 502."""
    real = f._s_implementing

    def flaky(job):
        real(job)
        f._s_implementing = real
        raise RuntimeError("[backend:cursor] cursor-agent error (exit 1): Error: [unavailable] HTTP 502")
    f._s_implementing = flaky


HALF = [[{"name": "write_file", "input": {"path": "invoice/half_done.py", "content": "x = 1\n"}}],
        [{"name": "finish", "input": {"summary": "partway"}}]]


def test_a_transient_failure_continues_the_half_finished_work(invoice_repo, state, store):
    f = factory(invoice_repo, state, store, provider(implementer=[HALF, IMPL]))
    job_id, _ = f.submit(item())
    _interrupted_after_half_the_work(f)
    assert f.run_job(job_id) == "failed"
    tasks = []
    real_task = f._task
    f._task = lambda job, extra="": tasks.append(real_task(job, extra)) or tasks[-1]
    assert f.auto_retry(job_id) == "implementing"
    assert events(store, job_id, "sandbox.kept")[0]["payload"]["files"] == ["invoice/half_done.py"]
    assert not events(store, job_id, "sandbox.reset")
    assert f.run_job(job_id) == "awaiting_review"
    assert "cut off by a backend error" in tasks[0] and "invoice/half_done.py" in tasks[0]
    assert "invoice/half_done.py" in store.job(job_id)["data"]["changed"]
    assert store.job(job_id)["data"]["partial_work"] == []


def test_a_human_retry_still_starts_from_the_base(invoice_repo, state, store):
    f = factory(invoice_repo, state, store, provider(implementer=[HALF, IMPL]))
    job_id, _ = f.submit(item())
    _interrupted_after_half_the_work(f)
    assert f.run_job(job_id) == "failed"
    assert f.retry(job_id) == "implementing"
    assert events(store, job_id, "sandbox.reset")
    assert f.run_job(job_id) == "awaiting_review"
    assert "invoice/half_done.py" not in store.job(job_id)["data"]["changed"]
