"""Per-job grants: a human lets one job edit named existing test files, and nothing else."""

import pytest

from asf.broker import ToolBroker
from asf.gates import GateRunner
from conftest import invoice_config, load_cfg, sh
from test_broker import sandbox
from test_pipeline import IMPL, factory, item, provider

TOTALS = "tests/test_totals.py"
EXTRA_ASSERT = {"path": TOTALS, "old_text": "    assert total_cents([(100, 2), (50, 1)]) == 250\n",
                "new_text": "    assert total_cents([(100, 2), (50, 1)]) == 250\n    assert total_cents([]) == 0\n"}


def test_grant_lifts_only_the_existing_test_rule(invoice_repo, state):
    cfg = load_cfg(invoice_config())
    sb = sandbox(invoice_repo, state, cfg)
    plain = ToolBroker(sb, cfg, "implementer", tier=3)
    out, err = plain.call("edit_file", EXTRA_ASSERT)
    assert err and "existing test" in out and "asf allow" in out

    granted = ToolBroker(sb, cfg, "implementer", tier=3, grants=[TOTALS, "factory.yaml", ".env"])
    out, err = granted.call("edit_file", EXTRA_ASSERT)
    assert not err, out
    # Still never: deleting an existing test, another existing test, protected and secret files.
    out, err = granted.call("delete_file", {"path": TOTALS})
    assert err and "never allowed" in out
    out, err = granted.call("write_file", {"path": "tests/__init__.py", "content": "x = 1\n"})
    assert err and "existing test" in out
    out, err = granted.call("write_file", {"path": "factory.yaml", "content": "x"})
    assert err and "protected" in out
    out, err = granted.call("read_file", {"path": ".env"})
    assert err
    # Reviewers still cannot write anything.
    out, err = ToolBroker(sb, cfg, "reviewer", tier=3, grants=[TOTALS]).call(
        "write_file", {"path": TOTALS, "content": "x"})
    assert err


def test_gate_accepts_granted_edits_but_not_weaker_tests(invoice_repo, state):
    cfg = load_cfg(invoice_config())
    sb = sandbox(invoice_repo, state, cfg)
    path = sb.root / TOTALS
    path.write_text(path.read_text() + "\n\ndef test_empty():\n    assert total_cents([]) == 0\n")

    ungranted = GateRunner(cfg, sb, 3, state).run(["test_integrity"])[0]
    assert ungranted.status == "fail" and "modified existing test" in ungranted.detail
    granted = GateRunner(cfg, sb, 3, state, grants=[TOTALS]).run(["test_integrity"])[0]
    assert granted.status == "pass" and "human grant: tests/test_totals.py" in granted.detail

    path.write_text("from invoice.totals import total_cents\n\n\ndef test_total():\n    pass\n")
    weakened = GateRunner(cfg, sb, 3, state, grants=[TOTALS]).run(["test_integrity"])[0]
    assert weakened.status == "fail" and "lost 1 assertions" in weakened.detail


def test_grant_validates_paths_and_needs_a_reason(invoice_repo, state, store):
    f = factory(invoice_repo, state, store, provider())
    job_id, _ = f.submit(item())
    for bad, why in [("/etc/passwd", "repository-relative"), ("../x/tests/a.py", "inside the repository"),
                     ("tests/*.py", "exact files"), ("invoice/totals.py", "not a test path"),
                     ("tests/test_missing.py", "does not exist")]:
        with pytest.raises(ValueError, match=why):
            f.grant(job_id, [bad], "lead", "because")
    with pytest.raises(ValueError, match="reason"):
        f.grant(job_id, [TOTALS], "lead", " ")

    assert f.grant(job_id, ["./" + TOTALS], "lead", "helper must match real storage") == [TOTALS]
    assert f.grant(job_id, [TOTALS], "lead", "updated reason") == [TOTALS]  # no duplicates
    grants = store.job(job_id)["data"]["grants"]
    assert [(g["path"], g["by"], g["reason"]) for g in grants] == [(TOTALS, "lead", "updated reason")]
    assert f.revoke(job_id, [TOTALS], "lead") == [TOTALS]
    assert store.job(job_id)["data"]["grants"] == []
    kinds = [e["type"] for e in store.events(job_id) if e["type"].startswith("grant.")]
    assert kinds == ["grant.add", "grant.add", "grant.revoke"]


def test_granted_job_edits_the_file_end_to_end(invoice_repo, state, store):
    impl = [[{"name": "edit_file", "input": EXTRA_ASSERT}]] + IMPL
    f = factory(invoice_repo, state, store, provider(implementer=[impl]))
    job_id, _ = f.submit(item())
    f.grant(job_id, [TOTALS], "lead", "cover the empty case in the existing suite")
    assert f.run_job(job_id) == "done"
    job = store.job(job_id)
    branch = job["data"]["delivery"]["branch"]
    assert TOTALS in sh(invoice_repo, "git", "show", "--name-only", "--format=", branch)
    gate = next(g for g in job["data"]["gates"] if g["name"] == "test_integrity")
    assert gate["status"] == "pass" and "human grant" in gate["detail"]
    report = open(job["data"]["report"]).read()
    assert "Human grants" in report and "cover the empty case" in report
    allowed = [e for e in store.events(job_id) if e["type"] == "tool.call"
               and e["payload"]["tool"] == "edit_file" and e["payload"]["args"]["path"] == TOTALS]
    assert allowed and allowed[0]["payload"]["decision"] == "allow"


def test_without_a_grant_the_same_edit_is_denied(invoice_repo, state, store):
    impl = [[{"name": "edit_file", "input": EXTRA_ASSERT}]] + IMPL
    f = factory(invoice_repo, state, store, provider(implementer=[impl]))
    job_id, _ = f.submit(item())
    assert f.run_job(job_id) == "done"
    branch = store.job(job_id)["data"]["delivery"]["branch"]
    assert TOTALS not in sh(invoice_repo, "git", "show", "--name-only", "--format=", branch)


def test_agents_are_told_their_own_test_files_are_not_existing_tests(invoice_repo, state):
    from asf.agents.roles import OWN_TESTS, PromptBuilder
    from asf.orchestrator import _grant_note
    prompts = PromptBuilder(load_cfg(invoice_config()), invoice_repo)
    for role in ("implementer", "ci_fixer"):
        assert OWN_TESTS in prompts.system(role, 3)
        assert "you may edit them at this tier" in prompts.system(role, 0)
    note = _grant_note("implementer", [TOTALS])
    assert TOTALS in note and "never restricted" in note and "merge or delete" in note


def test_job_created_tests_stay_editable_even_after_delivery(invoice_repo, state):
    cfg = load_cfg(invoice_config())
    sb = sandbox(invoice_repo, state, cfg)
    impl = ToolBroker(sb, cfg, "implementer", tier=3)
    for path in ("tests/test_a.py", "tests/test_b.py"):
        out, err = impl.call("write_file", {"path": path, "content": "def test_x():\n    assert 1\n"})
        assert not err, out
    sb.commit("first delivery")  # a revision continues on top of this; the job's base is unchanged
    reviser = ToolBroker(sb, cfg, "implementer", tier=3)
    out, err = reviser.call("edit_file", {"path": "tests/test_a.py", "old_text": "assert 1",
                                          "new_text": "assert 1\n    assert 2"})
    assert not err, out
    out, err = reviser.call("delete_file", {"path": "tests/test_b.py"})
    assert not err, out
    out, err = reviser.call("edit_file", EXTRA_ASSERT)
    assert err and "when this job started" in out and "this job created are not affected" in out
