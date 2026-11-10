import json

from asf.adapters.base import AdapterContext
from asf.adapters.sandbox.local_worktree import LocalWorktreeProvider
from asf.gates import GateRunner, failing_ids
from conftest import PY, invoice_config, load_cfg, make_repo


def make_sb(repo, state, cfg, job="JOB-g"):
    ctx = AdapterContext(repo=repo, state_dir=state, config=cfg)
    prov = LocalWorktreeProvider(cfg.get("execution"), ctx)
    return prov, prov.create(job, "main", f"factory/{job}")


def test_failing_id_parsers():
    out = "FAIL src/a.test.ts\nFAILED tests/test_x.py::test_y - assert\n--- FAIL: TestZ (0.0s)\n" \
          "server/a.ts(3,4): error TS2322: Type"
    ids = failing_ids(out)
    assert {"src/a.test.ts", "tests/test_x.py::test_y", "TestZ", "server/a.ts:TS2322"} <= ids


def test_static_gates(invoice_repo, state):
    cfg = load_cfg(invoice_config())
    _, sb = make_sb(invoice_repo, state, cfg)
    (sb.root / "invoice" / "totals.py").write_text("X = 'sk-ant-api03-aaaaaaaaaaaaaaaaaaaa'\n")
    results = {r.name: r for r in GateRunner(cfg, sb, 2, state).run(
        ["secret_scan", "test_integrity", "diff_size"])}
    assert results["secret_scan"].status == "fail"
    assert results["test_integrity"].status == "fail"  # code changed, no tests
    assert results["diff_size"].status == "pass"


def test_dependency_policy(tmp_path, state):
    repo = make_repo(tmp_path / "node", {"package.json": json.dumps({"dependencies": {"a": "1"}})})
    cfg = load_cfg(invoice_config())
    _, sb = make_sb(repo, state, cfg)
    (sb.root / "package.json").write_text(json.dumps({"dependencies": {"a": "1", "left-pad": "1"}}))
    low = GateRunner(cfg, sb, 1, state).run(["dependency_policy"])[0]
    high = GateRunner(cfg, sb, 2, state).run(["dependency_policy"])[0]
    assert low.status == "fail" and "left-pad" in low.detail
    assert high.status == "pass" and "flagged" in high.detail


def test_preexisting_failures_are_not_blamed(tmp_path, state):
    files = {"m.py": "def f():\n    return 1\n",
             "tests/test_old.py": "def test_broken():\n    assert False\n"}
    repo = make_repo(tmp_path / "pre", files)
    cfg = load_cfg(invoice_config())
    prov, sb = make_sb(repo, state, cfg)
    (sb.root / "m.py").write_text("def f():\n    return 2\n")
    (sb.root / "tests" / "test_new.py").write_text("from m import f\n\ndef test_f():\n    assert f() == 2\n")
    runner = GateRunner(cfg, sb, 2, state, base_sandbox=lambda: prov.base_checkout(sb.base_sha))
    res = [r for r in runner.run(["test"]) if r.name == "test"][0]
    assert res.status == "preexisting", res.detail
    # A new failure on top of the old one is still caught.
    (sb.root / "tests" / "test_new2.py").write_text("def test_g():\n    assert 1 == 2\n")
    res = [r for r in GateRunner(cfg, sb, 2, state, base_sandbox=lambda: prov.base_checkout(sb.base_sha))
           .run(["test"]) if r.name == "test"][0]
    assert res.status == "fail" and "test_new2" in res.detail


def _delivered_job_tests(tmp_path, state, files):
    """A sandbox whose branch tip holds tests the job delivered in an earlier revision."""
    repo = make_repo(tmp_path / "rev", {"m.py": "X = 1\n", "tests/test_old.py": "def test_old():\n    assert 1\n"})
    cfg = load_cfg(invoice_config())
    _, sb = make_sb(repo, state, cfg)
    for rel, text in files.items():
        (sb.root / rel).write_text(text)
    return cfg, sb, sb.commit("delivered")


def _case(name, n_asserts=1):
    return f"def {name}():\n" + "    assert True\n" * n_asserts + "\n\n"


def test_job_tests_merged_duplicates_pass(tmp_path, state):
    cfg, sb, tip = _delivered_job_tests(tmp_path, state, {
        "tests/test_a.py": _case("test_x") + _case("test_y"),
        "tests/test_b.py": _case("test_x") + _case("test_z", 2)})
    # Revision: fold test_b into test_a, dropping the duplicate test_x.
    (sb.root / "tests" / "test_b.py").unlink()
    (sb.root / "tests" / "test_a.py").write_text(_case("test_x") + _case("test_y") + _case("test_z", 2))
    res = GateRunner(cfg, sb, 2, state, revision_base=tip).run(["test_integrity"])[0]
    assert res.status == "pass", res.detail
    assert "job tests: 4→3 cases (3→3 distinct), 5→4 assertions" in res.detail


def test_job_tests_dropped_cases_fail(tmp_path, state):
    cfg, sb, tip = _delivered_job_tests(tmp_path, state, {
        "tests/test_a.py": _case("test_x"),
        "tests/test_b.py": _case("test_w") + _case("test_z")})
    (sb.root / "tests" / "test_b.py").unlink()
    (sb.root / "tests" / "test_a.py").write_text(_case("test_x") + _case("test_more"))
    res = GateRunner(cfg, sb, 2, state, revision_base=tip).run(["test_integrity"])[0]
    assert res.status == "fail"
    assert "3→2 cases (3→2 distinct)" in res.detail and "test_w, test_z" in res.detail
    # Outside a revision the job's own files are simply new, as before.
    assert GateRunner(cfg, sb, 2, state).run(["test_integrity"])[0].status == "pass"


def test_job_tests_renamed_cases_need_their_assertions(tmp_path, state):
    cfg, sb, tip = _delivered_job_tests(tmp_path, state, {"tests/test_a.py": _case("test_x", 3)})
    (sb.root / "tests" / "test_a.py").write_text(_case("test_renamed", 3))
    ok = GateRunner(cfg, sb, 2, state, revision_base=tip).run(["test_integrity"])[0]
    assert ok.status == "pass" and "no longer present: test_x" in ok.detail
    (sb.root / "tests" / "test_a.py").write_text(_case("test_renamed", 1))
    weak = GateRunner(cfg, sb, 2, state, revision_base=tip).run(["test_integrity"])[0]
    assert weak.status == "fail" and "3→1 assertions" in weak.detail


def test_job_tests_revision_that_only_adds_passes(tmp_path, state):
    cfg, sb, tip = _delivered_job_tests(tmp_path, state, {
        "tests/test_a.py": _case("test_x"), "tests/test_b.py": _case("test_y")})
    (sb.root / "tests" / "test_c.py").write_text(_case("test_new"))
    (sb.root / "tests" / "test_a.py").write_text(_case("test_x", 2))
    res = GateRunner(cfg, sb, 2, state, revision_base=tip).run(["test_integrity"])[0]
    assert res.status == "pass" and "job tests: 2→3 cases" in res.detail, res.detail
