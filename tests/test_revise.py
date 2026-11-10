"""`asf revise`: a delivered, unmerged job goes back to work on its own branch with review notes."""

import pytest

from conftest import sh
from test_pipeline import APPROVE, IMPL, TESTER, factory, item, provider

NOTE = "tests/test_discount.py should also cover a 0% discount"
# The revision edits a test file the job itself added (allowed: it did not exist at base).
REVISE_IMPL = [
    [{"name": "read_file", "input": {"path": "tests/test_discount.py"}}],
    [{"name": "write_file", "input": {"path": "tests/test_discount.py", "content":
        "from invoice.totals import apply_discount\n\n\ndef test_rounds_half_up():\n"
        "    assert apply_discount(999, 50) == 500\n\n\ndef test_no_discount():\n"
        "    assert apply_discount(999, 0) == 999\n"}}],
    [{"name": "finish", "input": {"summary": "Added the 0% case the reviewer asked for."}}],
]
NO_CHANGE = [[{"name": "finish", "input": {"summary": "Nothing to change."}}]]


def recording(prov):
    """Keep every conversation so tests can read the task each agent was given."""
    convs = []
    orig = prov.conversation

    def conversation(*a, **kw):
        c = orig(*a, **kw)
        convs.append(c)
        return c
    prov.conversation = conversation
    return convs


def delivered(invoice_repo, state, store, **scripts):
    prov = provider(**scripts)
    f = factory(invoice_repo, state, store, prov)
    job_id, _ = f.submit(item())
    assert f.run_job(job_id) == "done"
    return f, prov, job_id


def test_revise_adds_a_commit_on_the_same_branch(invoice_repo, state, store):
    f, prov, job_id = delivered(invoice_repo, state, store,
                                implementer=[IMPL, REVISE_IMPL], tester=[TESTER, [[{"name": "finish", "input": {
                                    "summary": "0% case covered.", "tests_pass": True}}]]],
                                reviewer=[APPROVE, APPROVE], security=[APPROVE, APPROVE])
    first = store.job(job_id)
    branch, first_commit = first["data"]["branch"], first["data"]["commit"]
    convs = recording(prov)

    assert f.revise(job_id, [NOTE]) == "implementing"
    assert f.run_job(job_id) == "done"

    job = store.job(job_id)
    log = sh(invoice_repo, "git", "log", "--format=%H %s", f"main..{branch}").splitlines()
    assert len(log) == 2 and log[1].startswith(first_commit), "original commit kept, one commit added"
    msg = sh(invoice_repo, "git", "log", "-1", "--format=%B", branch)
    assert msg.startswith("Revise: ") and NOTE in msg and "Revision: 1" in msg
    assert "test_no_discount" in sh(invoice_repo, "git", "show", f"{branch}:tests/test_discount.py")
    # The implementer and the tester were told about the note; the change was reviewed again.
    implementer_task = next(c for c in convs if c.role == "implementer").inbox[0]
    tester_task = next(c for c in convs if c.role == "tester").inbox[0]
    assert NOTE in implementer_task and "do not start over" in implementer_task
    assert NOTE in tester_task
    assert [c.role for c in convs] == ["implementer", "tester", "reviewer", "security"]
    rev = job["data"]["revisions"][0]
    assert rev["commit"] == job["data"]["commit"] and rev["from_commit"] == first_commit
    report = open(job["data"]["report"]).read()
    assert "### Revisions" in report and NOTE in report
    assert any(e["type"] == "job.revise" for e in store.events(job_id))
    # The user's checkout is untouched.
    assert sh(invoice_repo, "git", "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


def test_revise_with_no_change_keeps_the_delivered_work(invoice_repo, state, store):
    f, _, job_id = delivered(invoice_repo, state, store, implementer=[IMPL, NO_CHANGE, NO_CHANGE])
    branch, first_commit = store.job(job_id)["data"]["branch"], store.job(job_id)["data"]["commit"]
    f.revise(job_id, [NOTE])
    assert f.run_job(job_id) == "needs_human"
    assert "made no changes" in store.job(job_id)["data"]["needs_human_reason"]
    # Retrying a revision restarts from the delivered commit, never from the base.
    assert f.retry(job_id) == "implementing"
    assert sh(invoice_repo, "git", "rev-parse", branch).strip() == first_commit
    assert f.run_job(job_id) == "needs_human"
    assert sh(invoice_repo, "git", "rev-parse", branch).strip() == first_commit


def test_revise_refusals(invoice_repo, state, store):
    f, _, job_id = delivered(invoice_repo, state, store)
    branch = store.job(job_id)["data"]["branch"]
    with pytest.raises(ValueError, match="at least one"):
        f.revise(job_id, ["  "])
    # Checked out in the user's own checkout: the factory cannot use it.
    sh(invoice_repo, "git", "checkout", "-q", branch)
    with pytest.raises(ValueError, match="checked out"):
        f.revise(job_id, [NOTE])
    sh(invoice_repo, "git", "checkout", "-q", "main")
    # Already merged by the human: further changes need a new work item.
    sh(invoice_repo, "git", "merge", "-q", "--no-ff", "--no-edit", branch)
    with pytest.raises(ValueError, match="already merged"):
        f.revise(job_id, [NOTE])
    # Not delivered yet.
    other, _ = f.submit(item(title="Another issue entirely"))
    with pytest.raises(ValueError, match="only delivered"):
        f.revise(other, [NOTE])
    assert store.job(job_id)["state"] == "done"
