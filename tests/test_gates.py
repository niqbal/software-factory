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
