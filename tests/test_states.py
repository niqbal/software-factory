"""Delivered is not done: jobs wait for review, notice merges, and verify them before 'done'."""

from conftest import sh
from test_pipeline import factory, item, provider


def delivered(repo, state, store, **cfg):
    f = factory(repo, state, store, provider(), **cfg)
    job_id, _ = f.submit(item())
    assert f.run_job(job_id) == "awaiting_review"
    return f, job_id


def test_a_human_merge_is_noticed_and_verified(invoice_repo, state, store):
    f, job_id = delivered(invoice_repo, state, store, **{"observe.post_merge": ["true"]})
    assert f.run_job(job_id) == "awaiting_review"  # nothing merged yet: still waiting
    branch = store.job(job_id)["data"]["branch"]
    sh(invoice_repo, "git", "merge", "-q", "--no-ff", "--no-edit", branch)  # merged outside the factory
    assert f.run_job(job_id) == "done"
    d = store.job(job_id)["data"]
    assert d["merged_by"] == "human" and d["post_merge"]["status"] == "pass" and d["outcome"] == "shipped"
    assert d["delivered_at"] <= d["merged_at"] <= d["done_at"]


def test_red_main_after_merge_is_not_done(invoice_repo, state, store):
    f, job_id = delivered(invoice_repo, state, store, **{"observe.post_merge": ["exit 3"]})
    assert f.merge_reviewed(job_id, by="owner") == "needs_human"
    job = store.job(job_id)
    assert "post-merge check failed" in job["data"]["needs_human_reason"]
    assert job["data"]["post_merge"]["status"] == "fail"


def test_merge_from_review_and_reject(invoice_repo, state, store):
    f, job_id = delivered(invoice_repo, state, store)
    assert f.merge_reviewed(job_id, by="owner") == "done"
    assert "Round" in sh(invoice_repo, "git", "log", "-3", "--format=%B", "main") or \
        "Discount" in sh(invoice_repo, "git", "log", "-3", "--format=%B", "main")
    other, _ = f.submit(item(title="Another issue entirely"))
    f.reject_job(other, by="owner", reason="not needed")
    assert store.job(other)["state"] == "rejected"


def test_deleted_branch_counts_as_abandoned(invoice_repo, state, store):
    f, job_id = delivered(invoice_repo, state, store)
    sh(invoice_repo, "git", "branch", "-D", store.job(job_id)["data"]["branch"])
    assert f.run_job(job_id) == "rejected"
    assert "abandoned" in store.job(job_id)["data"]["outcome"]


def test_legacy_delivered_jobs_migrate(invoice_repo, state, store):
    f, job_id = delivered(invoice_repo, state, store)
    store.update_job(job_id, "done", outcome="delivered for human merge")  # how old versions saved it
    assert f.migrate_legacy() == 1
    assert store.job(job_id)["state"] == "awaiting_review"


def test_owner_review_lists_owner_only_lines(invoice_repo, state, store):
    f, job_id = delivered(invoice_repo, state, store, **{"owner_review.paths": ["invoice/totals.py"]})
    d = store.job(job_id)["data"]
    owner = d["owner_review"]
    assert owner["files"] == ["invoice/totals.py"]           # the test file is not owner-only
    assert "invoice/totals.py" in owner["diff"] and "tests/test_discount.py" not in owner["diff"]
    assert "### Owner review" in open(d["report"]).read()


def test_owner_notes_are_extracted():
    from asf.orchestrator import owner_notes
    s = "Fixed rounding.\nOWNER REVIEW:\n- server.go: added TokenKey to Config (needed for signing)\n"
    assert owner_notes(s).startswith("- server.go: added TokenKey")
    assert owner_notes("no special changes") == ""
