from asf.adapters.base import AdapterContext
from asf.adapters.sandbox.local_worktree import LocalWorktreeProvider
from asf.broker import ToolBroker
from conftest import invoice_config, load_cfg


def sandbox(repo, state, cfg):
    ctx = AdapterContext(repo=repo, state_dir=state, config=cfg)
    return LocalWorktreeProvider(cfg.get("execution"), ctx).create("JOB-1", "main", "factory/test")


def test_role_permissions(invoice_repo, state):
    cfg = load_cfg(invoice_config())
    sb = sandbox(invoice_repo, state, cfg)
    impl = ToolBroker(sb, cfg, "implementer", tier=2)
    out, err = impl.call("write_file", {"path": "factory.yaml", "content": "x"})
    assert err and "protected" in out
    out, err = impl.call("write_file", {"path": "../escape.txt", "content": "x"})
    assert err and "escapes" in out
    out, err = impl.call("edit_file", {"path": "tests/test_totals.py", "old_text": "250", "new_text": "1"})
    assert err and "existing test" in out
    out, err = impl.call("delete_file", {"path": "tests/test_totals.py"})
    assert err and "never allowed" in out
    out, err = impl.call("write_file", {"path": "tests/test_discount.py", "content": "def test_x():\n    pass\n"})
    assert not err
    out, err = impl.call("run_command", {"command": "curl https://example.com"})
    assert err
    out, err = impl.call("run_command", {"command": "ls; rm -rf /"})
    assert err and "shell operators" in out
    out, err = impl.call("run_command", {"command": "git log --oneline"})
    assert not err and "init" in out
    out, err = impl.call("run_check", {"kind": "test", "args": "tests/test_totals.py"})
    assert not err and "PASSED" in out
    out, err = impl.call("submit_review", {"verdict": "approve", "summary": "x"})
    assert err and "not available" in out

    tester = ToolBroker(sb, cfg, "tester", tier=2)
    out, err = tester.call("write_file", {"path": "invoice/totals.py", "content": "x"})
    assert err and "only write test files" in out

    reviewer = ToolBroker(sb, cfg, "reviewer", tier=2)
    out, err = reviewer.call("write_file", {"path": "tests/test_y.py", "content": "x"})
    assert err


def test_secrets_never_reach_the_model(invoice_repo, state):
    cfg = load_cfg(invoice_config())
    sb = sandbox(invoice_repo, state, cfg)
    (sb.root / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-api03-zzzzzzzzzzzzzzzz\n")
    (sb.root / "config.py").write_text("TOKEN = 'ghp_abcdefghijklmnopqrstuvwxyz12'\n")
    impl = ToolBroker(sb, cfg, "implementer", tier=2)
    out, err = impl.call("read_file", {"path": ".env"})
    assert err and "secret-file" in out
    out, err = impl.call("read_file", {"path": "config.py"})
    assert "ghp_" not in out and "[REDACTED]" in out


def test_existing_tests_editable_at_tier0(invoice_repo, state):
    cfg = load_cfg(invoice_config())
    sb = sandbox(invoice_repo, state, cfg)
    impl = ToolBroker(sb, cfg, "implementer", tier=0)
    out, err = impl.call("edit_file", {"path": "tests/test_totals.py", "old_text": "250", "new_text": "250 "})
    assert not err
