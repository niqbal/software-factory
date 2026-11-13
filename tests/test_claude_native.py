"""Native Claude Code sessions, driven by a fake `claude` that really calls the hook and the MCP
server it is given, so the bridge, the policy and the audit run for real (no model, no network)."""
import json
import os
import stat
import sys
import textwrap

import pytest

from asf.adapters.base import AdapterContext
from asf.adapters.models.claude_code import ClaudeCodeError
from asf.adapters.models.claude_native import ClaudeNativeProvider
from asf.adapters.models.pool import PoolProvider
from asf.adapters.sandbox.local_worktree import LocalWorktreeProvider
from asf.agents.roles import native_backend, run_agent
from asf.broker import ToolBroker
from conftest import invoice_config, load_cfg

# Plays one scripted session per run. Each step is {"tool": name, "input": {...}} for a built-in
# tool (asks the hook, then does it when allowed) or {"mcp": name, "input": {...}} for the asf server.
FAKE = textwrap.dedent('''\
    #!{py}
    import json, os, subprocess, sys
    args = sys.argv[1:]
    def arg(name):
        return args[args.index(name) + 1] if name in args else None
    prompt = sys.stdin.read()
    log = os.environ["FAKE_LOG"]
    runs = [json.loads(l) for l in open(log)] if os.path.exists(log) else []
    script = json.load(open(os.environ["FAKE_SCRIPT"]))[len(runs)]
    hook = json.load(open(arg("--settings")))["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    server = json.load(open(arg("--mcp-config")))["mcpServers"]["asf"]
    mcp = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    def rpc(i, method, params):
        mcp.stdin.write(json.dumps({{"jsonrpc": "2.0", "id": i, "method": method, "params": params}}) + "\\n")
        mcp.stdin.flush()
        return json.loads(mcp.stdout.readline())
    rpc(1, "initialize", {{"protocolVersion": "2025-06-18"}})
    listed = [t["name"] for t in rpc(2, "tools/list", {{}})["result"]["tools"]]
    out = []
    for n, step in enumerate(script.get("steps", [])):
        print(json.dumps({{"type": "assistant", "message": {{"content": [{{"type": "tool_use",
              "name": step.get("tool") or "mcp__asf__" + step["mcp"], "input": step["input"]}}]}}}}), flush=True)
        if "mcp" in step:
            r = rpc(10 + n, "tools/call", {{"name": step["mcp"], "arguments": step["input"]}})["result"]
            out.append({{"mcp": step["mcp"], "text": r["content"][0]["text"], "error": r["isError"]}})
            continue
        event = {{"tool_name": step["tool"], "tool_input": step["input"], "transcript_path": "/nowhere/t.jsonl"}}
        d = json.loads(subprocess.run(hook, shell=True, input=json.dumps(event), capture_output=True, text=True).stdout)
        decision = d["hookSpecificOutput"]["permissionDecision"]
        out.append({{"tool": step["tool"], "decision": decision, "reason": d["hookSpecificOutput"]["permissionDecisionReason"]}})
        if decision == "allow":
            i = step["input"]
            if step["tool"] == "Write":
                open(i["file_path"], "w").write(i["content"])
            elif step["tool"] == "Bash":
                subprocess.run(i["command"], shell=True, capture_output=True)
    mcp.stdin.close()
    with open(log, "a") as f:
        f.write(json.dumps({{"args": args, "prompt": prompt, "listed": listed, "out": out,
                            "settings": json.load(open(arg("--settings"))),
                            "env": sorted(os.environ)}}) + "\\n")
    print(json.dumps({{"type": "result", "subtype": script.get("subtype", "success"), "is_error": script.get("is_error", False),
                      "result": script.get("result", "done"), "session_id": "S-" + str(len(runs) + 1), "num_turns": 3,
                      "total_cost_usd": 0.25, "usage": {{"input_tokens": 10, "output_tokens": 20,
                      "cache_read_input_tokens": 300, "cache_creation_input_tokens": 0}},
                      "modelUsage": {{"claude-opus-5-5": {{}}}}}}), flush=True)
''')


@pytest.fixture
def fake(tmp_path, monkeypatch):
    exe = tmp_path / "claude"
    exe.write_text(FAKE.format(py=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    log, script = tmp_path / "log.jsonl", tmp_path / "script.json"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_SCRIPT", str(script))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    # sandbox_env keeps only a few variables; the fake needs to find its log and script.
    monkeypatch.setattr("asf.adapters.models.claude_native.AUTH_ENV", ("FAKE_LOG", "FAKE_SCRIPT"))

    def setup(sessions, **settings):
        script.write_text(json.dumps(sessions))
        log.unlink(missing_ok=True)
        return ClaudeNativeProvider({"binary": str(exe), **settings})

    def runs():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return setup, runs


@pytest.fixture
def sb(invoice_repo, state):
    cfg = load_cfg(invoice_config())
    ctx = AdapterContext(repo=invoice_repo, state_dir=state, config=cfg)
    return cfg, LocalWorktreeProvider(cfg.get("execution"), ctx).create("JOB-1", "main", "factory/test")


def charger():
    turns = []
    return turns, (lambda role, turn: turns.append((role, turn)))


def finish(**extra):
    return {"mcp": "finish", "input": {"summary": "fixed the rounding", **extra}}


def test_implementer_session_policy_checks_and_finish(fake, sb):
    cfg, box = sb
    root = str(box.root)
    setup, runs = fake
    p = setup([{"steps": [
        {"tool": "Write", "input": {"file_path": f"{root}/invoice/discount.py", "content": "X = 1\n"}},
        {"tool": "Write", "input": {"file_path": f"{root}/factory.yaml", "content": "x"}},
        {"tool": "Write", "input": {"file_path": f"{root}/tests/test_totals.py", "content": "x"}},
        {"tool": "Write", "input": {"file_path": f"{root}/.claude/settings.json", "content": "{}"}},
        {"tool": "Read", "input": {"file_path": f"{root}/.env"}},
        {"tool": "Read", "input": {"file_path": "/etc/hosts"}},
        {"tool": "Read", "input": {"file_path": "/nowhere/tool-results/big.txt"}},
        {"tool": "Bash", "input": {"command": "curl https://example.com"}},
        {"tool": "Bash", "input": {"command": "git commit -am x"}},
        {"tool": "Bash", "input": {"command": "cat .env"}},
        {"tool": "Bash", "input": {"command": "python -c 'print(1)' && git status"}},
        {"tool": "WebFetch", "input": {"url": "https://example.com"}},
        {"tool": "Task", "input": {"prompt": "look around"}},
        {"mcp": "run_check", "input": {"kind": "test", "args": "tests/test_totals.py"}},
        finish(acceptance_met=["rounds half up"], tests_added=["tests/test_discount.py"]),
    ]}])
    broker = ToolBroker(box, cfg, "implementer", tier=2)
    turns, charge = charger()
    res = run_agent(p, cfg, "implementer", "ROLE PROMPT", "do the task", broker, charge,
                    budget_left=(3.5, 30))
    run = runs()[0]
    decisions = [(o.get("tool"), o.get("decision")) for o in run["out"] if "tool" in o]
    assert decisions == [("Write", "allow"), ("Write", "deny"), ("Write", "deny"), ("Write", "deny"),
                         ("Read", "deny"), ("Read", "deny"), ("Read", "allow"), ("Bash", "deny"),
                         ("Bash", "deny"), ("Bash", "deny"), ("Bash", "allow"), ("WebFetch", "deny"),
                         ("Task", "allow")]
    reasons = " | ".join(o.get("reason", "") for o in run["out"])
    assert "protected" in reasons and "existing test" in reasons and "outside the job's checkout" in reasons
    checks = [o for o in run["out"] if o.get("mcp") == "run_check"]
    assert "PASSED" in checks[0]["text"] and not checks[0]["error"]
    assert run["listed"] == ["run_check", "finish"]

    assert res.ok and res.summary == "fixed the rounding" and res.data["native"]
    assert res.data["acceptance_met"] == ["rounds half up"] and res.data["session_id"] == "S-1"
    assert res.data["files_touched"] == ["invoice/discount.py"] and res.data["denials"] == 9
    assert res.cost_usd == 0.25 and res.turns == 3 and res.input_tokens == 310
    assert [(r, t.cost_usd) for r, t in turns] == [("implementer", 0.25)]
    assert (box.root / "invoice/discount.py").read_text() == "X = 1\n"

    a = run["args"]
    assert "--system-prompt" not in a and a[a.index("--append-system-prompt") + 1].startswith("ROLE PROMPT")
    assert "mcp__asf__finish" in a[a.index("--append-system-prompt") + 1]
    assert a[a.index("--setting-sources") + 1] == "project" and a[a.index("--max-budget-usd") + 1] == "3.50"
    assert a[a.index("--model") + 1] == "claude-opus-5-5" and "--resume" not in a
    assert set(a[a.index("--tools") + 1].split(",")) >= {"Read", "Edit", "Write", "Bash", "Grep", "Glob"}
    assert run["prompt"] == "do the task"
    assert "ANTHROPIC_API_KEY" not in run["env"] and "ASF_BRIDGE_TOKEN" in run["env"]
    assert "Read(**/.env)" in run["settings"]["permissions"]["deny"]


def test_shell_changes_are_audited_and_reverted(fake, sb):
    cfg, box = sb
    setup, runs = fake
    (box.root / "invoice/totals.py").write_text("# changed by the implementer earlier\n")
    before_totals = (box.root / "invoice/totals.py").read_text()
    p = setup([{"steps": [
        {"tool": "Bash", "input": {"command": "echo 'def test_new(): pass' > tests/test_new.py"}},
        {"tool": "Bash", "input": {"command": "echo hacked > invoice/totals.py"}},
        {"tool": "Bash", "input": {"command": "rm tests/test_totals.py"}},
        finish(defect_found=""),
    ]}])
    res = run_agent(p, cfg, "tester", "TESTER", "test it", ToolBroker(box, cfg, "tester", tier=2), charger()[1])
    assert res.ok
    assert sorted(res.data["reverted"]) == ["invoice/totals.py", "tests/test_totals.py"]
    assert (box.root / "invoice/totals.py").read_text() == before_totals  # back to before the session
    assert (box.root / "tests/test_totals.py").exists()
    assert (box.root / "tests/test_new.py").exists()


def test_nudged_once_then_resumed(fake, sb):
    cfg, box = sb
    setup, runs = fake
    p = setup([{"steps": []}, {"steps": [finish()]}])
    turns, charge = charger()
    res = run_agent(p, cfg, "implementer", "S", "task", ToolBroker(box, cfg, "implementer", tier=2), charge,
                    resume="EARLIER")
    first, second = runs()
    assert first["args"][first["args"].index("--resume") + 1] == "EARLIER"
    assert second["args"][second["args"].index("--resume") + 1] == "S-1"
    assert "mcp__asf__finish" in second["prompt"]
    assert res.ok and res.data["nudged"] and res.turns == 6 and len(turns) == 2

    p = setup([{"steps": []}, {"steps": []}])
    res = run_agent(p, cfg, "implementer", "S", "task", ToolBroker(box, cfg, "implementer", tier=2), charger()[1])
    assert not res.ok and "without finishing" in res.summary


def test_errors_fail_but_session_limits_just_stop(fake, sb):
    cfg, box = sb
    setup, _ = fake
    p = setup([{"is_error": True, "subtype": "success", "result": "Claude AI usage limit reached|1767225600"}])
    with pytest.raises(ClaudeCodeError, match="usage limit"):
        run_agent(p, cfg, "implementer", "S", "t", ToolBroker(box, cfg, "implementer", tier=2), charger()[1])

    p = setup([{"is_error": True, "subtype": "error_max_turns"}])
    res = run_agent(p, cfg, "implementer", "S", "t", ToolBroker(box, cfg, "implementer", tier=2), charger()[1])
    assert not res.ok and "error_max_turns" in res.summary


def test_routing_reviewers_docker_and_pools(fake, sb, monkeypatch):
    cfg, box = sb
    setup, runs = fake
    p = setup([])
    assert native_backend(p, cfg, "implementer", box)[0] is p
    assert native_backend(p, cfg, "reviewer", box) is None  # reviewers keep the tool protocol
    box.host_shell = False  # e.g. Docker: a shell here would run outside the container
    assert native_backend(p, cfg, "implementer", box) is None
    box.host_shell = True

    pool = PoolProvider({"backends": {
        "claude": {"provider": "claude_code_native", "binary": p.binary, "default": {"model": "claude-sonnet-5-5"}}}})
    prov, spec, name = native_backend(pool, cfg, "tester", box)
    assert name == "claude" and spec["model"] == "claude-sonnet-5-5"
    setup([{"is_error": True, "result": "API Error: 529 overloaded"}])
    with pytest.raises(RuntimeError, match=r"\[backend:claude\].*overloaded"):
        run_agent(pool, cfg, "tester", "S", "t", ToolBroker(box, cfg, "tester", tier=2), charger()[1])
