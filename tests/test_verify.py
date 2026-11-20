"""The verifier and the MCP server it backs: gates over a checkout the factory does not own."""

import json
from io import StringIO

import pytest

from asf import mcp_server
from asf.verify import VerifyError, Verifier
from conftest import invoice_config, make_repo, sh

REVIEW = {"verdict": "request_changes", "summary": "the discount still truncates",
          "findings": [{"severity": "blocking", "file": "invoice/totals.py", "line": 8,
                        "issue": "int() truncates instead of rounding half up"}]}


def commit_config(repo, text=None):
    (repo / "factory.yaml").write_text(text or invoice_config())
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-q", "-m", "config")
    return repo


def test_reads_the_working_tree_without_staging_in_it(invoice_repo, state):
    commit_config(invoice_repo)
    (invoice_repo / "invoice" / "totals.py").write_text("X = 1\n")
    (invoice_repo / "extra.py").write_text("Y = 2\n")
    sb = Verifier.open(invoice_repo, state_dir=state).sandbox()

    assert sb.changed_files() == ["extra.py", "invoice/totals.py"]   # untracked files included
    assert "extra.py" in sb.diff()
    assert sb.file_at_base("invoice/totals.py").startswith("def total_cents")
    # The developer's own index is untouched: the new file is still merely untracked.
    porcelain = sh(invoice_repo, "git", "status", "--porcelain")
    assert "?? extra.py" in porcelain and " M invoice/totals.py" in porcelain


def test_the_verifier_never_writes(invoice_repo, state):
    commit_config(invoice_repo)
    sb = Verifier.open(invoice_repo, state_dir=state).sandbox()
    with pytest.raises(VerifyError):
        sb.commit("no")
    with pytest.raises(VerifyError):
        sb.reset()
    assert sb.host_shell is False   # so no native agent session is ever started in this checkout


def test_static_gates_over_the_working_tree(invoice_repo, state):
    commit_config(invoice_repo)
    # Assembled at runtime so the secret gate has nothing to find in this file's own diff.
    fake_key = "sk-ant-" + "api03-" + "a" * 20
    (invoice_repo / "invoice" / "totals.py").write_text(f"KEY = '{fake_key}'\n")
    data = Verifier.open(invoice_repo, state_dir=state).static()

    status = {r["gate"]: r["status"] for r in data["results"]}
    assert status["secret_scan"] == "fail"
    assert status["diff_size"] == "pass"
    assert data["verdict"] == "blocked" and data["failed"] == 2   # secrets, and no test for the change
    assert data["compared_with_base"] is False   # static gates run no commands
    assert "secret" in mcp_server.fmt_gates(data).lower()


def test_base_comparison_separates_preexisting_failures(tmp_path, state):
    repo = commit_config(make_repo(tmp_path / "pre", {
        "m.py": "def f():\n    return 1\n",
        "tests/test_old.py": "def test_broken():\n    assert False\n"}))
    (repo / "m.py").write_text("def f():\n    return 2\n")
    (repo / "tests" / "test_new.py").write_text("from m import f\n\ndef test_f():\n    assert f() == 2\n")

    data = Verifier.open(repo, state_dir=state).gates(names=["test"])
    result = [r for r in data["results"] if r["gate"] == "test"][0]
    assert result["status"] == "preexisting", result["detail"]
    assert data["compared_with_base"] is True and data["verdict"] == "clean"

    # A failure the change itself introduces is still blamed on it.
    (repo / "tests" / "test_newer.py").write_text("def test_g():\n    assert 1 == 2\n")
    data = Verifier.open(repo, state_dir=state).gates(names=["test"])
    result = [r for r in data["results"] if r["gate"] == "test"][0]
    assert result["status"] == "fail" and "test_newer" in result["detail"]
    assert data["verdict"] == "blocked" and result["log"]


def test_unknown_gate_names_are_reported_not_run(invoice_repo, state):
    commit_config(invoice_repo)
    (invoice_repo / "invoice" / "totals.py").write_text("X = 1\n")
    data = Verifier.open(invoice_repo, state_dir=state).gates(names=["secret_scan", "vibes"])
    assert data["unknown_gates"] == ["vibes"]
    assert [r["gate"] for r in data["results"]] == ["secret_scan", "vibes"]


def test_repo_without_factory_yaml_is_detected(invoice_repo, state):
    (invoice_repo / "invoice" / "totals.py").write_text("X = 1\n")
    v = Verifier.open(invoice_repo, state_dir=state)
    data = v.checks()
    assert v.configured is False and "detected" in data["config"]
    assert data["changed_files"] == ["invoice/totals.py"]
    assert "secret_scan" in data["gates"]


def test_base_defaults_to_the_merge_base_of_the_branch(invoice_repo, state):
    commit_config(invoice_repo)
    head = sh(invoice_repo, "git", "rev-parse", "HEAD").strip()
    sh(invoice_repo, "git", "checkout", "-q", "-b", "feature")
    (invoice_repo / "invoice" / "totals.py").write_text("X = 1\n")
    sh(invoice_repo, "git", "commit", "-qam", "committed on the branch")
    (invoice_repo / "extra.py").write_text("Y = 2\n")

    v = Verifier.open(invoice_repo, state_dir=state)
    # The whole branch is verified, committed work included, not just what is uncommitted.
    assert v.sandbox().base_sha == head
    assert v.checks()["changed_files"] == ["extra.py", "invoice/totals.py"]
    # An explicit base narrows it.
    assert v.checks(base="HEAD")["changed_files"] == ["extra.py"]


def test_review_reads_the_change_cold(invoice_repo, state):
    scripts = {"reviewer": [[[{"name": "submit_review", "input": REVIEW}]]]}
    commit_config(invoice_repo, invoice_config(models={
        "provider": "scripted", "scripts": scripts,
        "roles": {"reviewer": {"model": "scripted-1", "effort": "high"}}}))
    (invoice_repo / "invoice" / "totals.py").write_text("def apply_discount(c, p):\n    return int(c)\n")

    data = Verifier.open(invoice_repo, state_dir=state).review(intent="round the discount half up")
    assert data["ok"] is True and data["verdict"] == "request_changes"
    assert data["findings"][0]["severity"] == "blocking"
    assert data["role"] == "reviewer" and data["model"] == "scripted-1"
    assert "truncates" in mcp_server.fmt_review(data)


def test_review_needs_a_change_and_a_review_role(invoice_repo, state):
    commit_config(invoice_repo)
    v = Verifier.open(invoice_repo, state_dir=state)
    assert v.review()["verdict"] == "approve"      # nothing changed: no model time spent
    with pytest.raises(VerifyError):
        v.review(role="implementer")


def test_mcp_stdio_roundtrip(invoice_repo, state):
    commit_config(invoice_repo)
    (invoice_repo / "invoice" / "totals.py").write_text("X = 1\n")
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "list_checks", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "check_diff", "arguments": {"tier": 0}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 6, "method": "no/such", "params": {}},
        {"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {}},
    ]
    out = StringIO()
    mcp_server.Server(invoice_repo, state=str(state)).serve(
        StringIO("\n".join(json.dumps(r) for r in requests)), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]

    assert [r["id"] for r in replies] == [1, 2, 3, 4, 5, 6, 7]   # the notification is not answered
    assert replies[0]["result"]["serverInfo"]["name"] == "asf-verify"
    tools = replies[1]["result"]["tools"]
    assert [t["name"] for t in tools] == ["list_checks", "check_diff", "run_gates", "review_diff"]
    assert all(t["description"] and t["inputSchema"]["type"] == "object" for t in tools)
    assert "invoice/totals.py" in replies[2]["result"]["content"][0]["text"]
    assert replies[2]["result"]["isError"] is False
    # A blocked verdict is an answer, not a protocol error.
    assert replies[3]["result"]["isError"] is False
    assert "VERDICT" in replies[3]["result"]["content"][0]["text"]
    assert replies[4]["result"]["isError"] is True
    assert replies[5]["error"]["code"] == -32601
    assert replies[6]["result"] == {}


def test_a_broken_call_does_not_kill_the_server(tmp_path):
    text, is_error = mcp_server.Server(tmp_path).call("run_gates", {})
    assert is_error and "not a git repository" in text
