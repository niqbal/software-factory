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
    # Once main is fixed, a retry re-runs the checks; it does not rebuild the merged work.
    f.cfg.raw["observe"]["post_merge"] = ["true"]
    assert f.retry(job_id) == "merged"
    assert f.run_job(job_id) == "done"
    assert not [e for e in store.events(job_id) if e["type"] == "sandbox.reset"]


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


# ---- deploy: merged and verified is not live until the deploy steps pass ----------------

def _deploy_cfg(tmp_path, **step):
    log = tmp_path / "deploy.log"
    base = {"name": "publish", "run": f'echo "$ASF_JOB $ASF_SHA $(basename $PWD)" >> {log}',
            "preview": "echo would publish $ASF_CHANGED_PATHS"}
    return {"deploy.steps": [{**base, **step}]}, log


def test_owner_approved_deploy(invoice_repo, state, store, tmp_path):
    cfg, log = _deploy_cfg(tmp_path)
    f, job_id = delivered(invoice_repo, state, store, **cfg)
    assert f.merge_reviewed(job_id, by="owner") == "awaiting_deploy"
    d = store.job(job_id)["data"]["deploy"]
    assert d["steps"] == ["publish"] and "would publish invoice/" in d["preview"]
    assert f.run_job(job_id) == "awaiting_deploy" and not log.exists()  # waits for the owner
    assert f.approve_deploy(job_id, by="owner") == [job_id]
    assert f.run_job(job_id) == "done"
    tip = sh(invoice_repo, "git", "rev-parse", "main").strip()
    assert log.read_text().split() == [job_id, tip, "main"]  # ran on a clean worktree of main's tip
    d = store.job(job_id)["data"]
    assert d["deploy"]["status"] == "pass" and d["deploy"]["approved_by"] == "owner"
    assert "deployed" in d["outcome"] or d["outcome"] == "shipped"


def test_auto_deploy(invoice_repo, state, store, tmp_path):
    cfg, log = _deploy_cfg(tmp_path, approval="auto")
    f, job_id = delivered(invoice_repo, state, store, **cfg)
    assert f.merge_reviewed(job_id, by="owner") == "done" and log.exists()


def test_no_deploy_when_the_change_misses_its_paths(invoice_repo, state, store, tmp_path):
    cfg, log = _deploy_cfg(tmp_path, when=["web/**"])
    f, job_id = delivered(invoice_repo, state, store, **cfg)
    assert f.merge_reviewed(job_id, by="owner") == "done"  # nothing to deploy for this change
    assert not log.exists() and "deploy" not in store.job(job_id)["data"]


def test_failed_deploy_stops_and_retries(invoice_repo, state, store, tmp_path):
    flag = tmp_path / "ok"
    cfg, _ = _deploy_cfg(tmp_path, approval="auto", run=f"test -f {flag}")
    f, job_id = delivered(invoice_repo, state, store, **cfg)
    assert f.merge_reviewed(job_id, by="owner") == "needs_human"
    job = store.job(job_id)
    assert "deploy step publish failed" in job["data"]["needs_human_reason"]
    flag.write_text("")
    assert f.retry(job_id) == "deploying"
    assert f.run_job(job_id) == "done"


def test_one_deploy_covers_every_waiting_job(invoice_repo, state, store, tmp_path):
    cfg, log = _deploy_cfg(tmp_path)
    f, first = delivered(invoice_repo, state, store, **cfg)
    assert f.merge_reviewed(first, by="owner") == "awaiting_deploy"
    second, _ = f.submit(item(title="Another issue entirely"))
    d = store.job(first)["data"]  # a second merged job waiting for the same deploy
    store.update_job(second, "awaiting_deploy", sandbox=d["sandbox"], commit=d["commit"], deploy=d["deploy"])
    assert sorted(f.approve_deploy(second)) == sorted([first, second])
    assert f.run_job(first) == "done" and f.run_job(second) == "done"
    assert len(log.read_text().splitlines()) == 1  # main was deployed once


def test_deploy_config_is_validated():
    import pytest
    from asf import config as config_mod
    with pytest.raises(config_mod.ConfigError):
        config_mod.load(text="version: 1\ndeploy: {steps: [{name: x}]}\n")
    with pytest.raises(config_mod.ConfigError):
        config_mod.load(text="version: 1\ndeploy: {steps: [{name: x, run: y, approval: maybe}]}\n")


def test_post_merge_checks_get_the_sandbox_dependency_folders(invoice_repo, state, store):
    """Regression: post-merge ran in a bare worktree without clone_dirs, so a check like
    `.venv/bin/python -m pytest` failed with 'No such file or directory' on every merge."""
    (invoice_repo / ".gitignore").write_text("deps/\n")
    sh(invoice_repo, "git", "add", ".gitignore")
    sh(invoice_repo, "git", "commit", "-q", "-m", "ignore deps")
    (invoice_repo / "deps").mkdir()
    (invoice_repo / "deps" / "tool").write_text("installed\n")
    f, job_id = delivered(invoice_repo, state, store, **{
        "execution.clone_dirs": ["deps"], "observe.post_merge": ["test -f deps/tool"]})
    assert f.merge_reviewed(job_id, by="owner") == "done"
    assert store.job(job_id)["data"]["post_merge"]["status"] == "pass"
