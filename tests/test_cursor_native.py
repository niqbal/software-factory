"""Native Cursor sessions, driven by a fake `cursor-agent` that reads the hook and MCP server from
the checkout's .cursor/ (as Cursor does) and really calls them. No model, no network."""
import json
import os
import stat
import sys
import textwrap
import time

import pytest

from asf import limits
from asf.adapters.base import AdapterContext
from asf.adapters.models.cursor_cli import CursorError
from asf.adapters.models.cursor_native import CursorNativeProvider
from asf.adapters.models.native import BRIDGE, Bridge, NativePolicy
from asf.adapters.sandbox.local_worktree import LocalWorktreeProvider
from asf.agents.roles import native_backend, run_agent
from asf.broker import ToolBroker
from conftest import invoice_config, load_cfg

FAKE = textwrap.dedent('''\
    #!{py}
    import json, os, subprocess, sys
    args = sys.argv[1:]
    prompt = args[-1]
    log = os.environ["FAKE_LOG"]
    runs = [json.loads(l) for l in open(log)] if os.path.exists(log) else []
    script = json.load(open(os.environ["FAKE_SCRIPT"]))[len(runs)]
    hook = json.load(open(".cursor/hooks.json"))["hooks"]["preToolUse"][0]["command"]
    server = json.load(open(".cursor/mcp.json"))["mcpServers"]["asf"]
    # Like Cursor: MCP servers get a minimal environment plus the config's own env.
    mcp = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           text=True, env={{"PATH": os.environ.get("PATH", ""), **server.get("env", {{}})}})
    def rpc(i, method, params):
        mcp.stdin.write(json.dumps({{"jsonrpc": "2.0", "id": i, "method": method, "params": params}}) + "\\n")
        mcp.stdin.flush()
        return json.loads(mcp.stdout.readline())
    rpc(1, "initialize", {{"protocolVersion": "2025-06-18"}})
    listed = [t["name"] for t in rpc(2, "tools/list", {{}})["result"]["tools"]]
    out = []
    for n, step in enumerate(script.get("steps", [])):
        name = step.get("tool") or "MCP:" + step["mcp"]
        key = (step.get("tool") or "mcp").lower() + "ToolCall"
        print(json.dumps({{"type": "tool_call", "subtype": "started", "tool_call": {{key: {{"args": step["input"]}}}}}}), flush=True)
        event = {{"tool_name": name, "tool_input": step["input"]}}
        d = json.loads(subprocess.run(hook, shell=True, input=json.dumps(event), capture_output=True, text=True).stdout)
        out.append({{"tool": name, "decision": d["permission"], "reason": d.get("agent_message", "")}})
        if d["permission"] != "allow":
            continue
        i = step["input"]
        if "mcp" in step:
            r = rpc(10 + n, "tools/call", {{"name": step["mcp"], "arguments": i}})["result"]
            out[-1].update(text=r["content"][0]["text"], error=r["isError"])
        elif step["tool"] == "Write":
            open(i["file_path"], "w").write(i["content"])
        elif step["tool"] == "Shell":
            subprocess.run(i["command"], shell=True, capture_output=True)
    mcp.stdin.close()
    with open(log, "a") as f:
        f.write(json.dumps({{"args": args, "prompt": prompt, "listed": listed, "out": out,
                            "cursor_files": sorted(os.listdir(".cursor")), "env": sorted(os.environ)}}) + "\\n")
    print(json.dumps({{"type": "result", "subtype": script.get("subtype", "success"), "is_error": script.get("is_error", False),
                      "result": script.get("result", "done"), "session_id": "C-" + str(len(runs) + 1),
                      "usage": {{"inputTokens": 1000, "outputTokens": 200, "cacheReadTokens": 5000, "cacheWriteTokens": 0}}}}), flush=True)
''')


@pytest.fixture
def fake(tmp_path, monkeypatch):
    exe = tmp_path / "cursor-agent"
    exe.write_text(FAKE.format(py=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    log, script = tmp_path / "log.jsonl", tmp_path / "script.json"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_SCRIPT", str(script))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setattr("asf.adapters.models.cursor_native.AUTH_ENV", ("FAKE_LOG", "FAKE_SCRIPT"))

    def setup(sessions, **settings):
        script.write_text(json.dumps(sessions))
        log.unlink(missing_ok=True)
        return CursorNativeProvider({"binary": str(exe), "usd_per_mtok": {"in": 1, "out": 10}, **settings})

    def runs():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return setup, runs


@pytest.fixture
def sb(invoice_repo, state):
    cfg = load_cfg(invoice_config())
    ctx = AdapterContext(repo=invoice_repo, state_dir=state, config=cfg)
    return cfg, LocalWorktreeProvider(cfg.get("execution"), ctx).create("JOB-1", "main", "factory/test")


def charge(role, turn):
    pass


def test_cursor_session_policy_config_files_and_finish(fake, sb):
    cfg, box = sb
    root = str(box.root)
    setup, runs = fake
    (box.root / ".cursor/rules").mkdir(parents=True)
    (box.root / ".cursor/rules/style.mdc").write_text("use cents")
    (box.root / ".cursor/mcp.json").write_text('{"mcpServers": {}}')  # the project's own
    p = setup([{"steps": [
        {"tool": "Write", "input": {"file_path": f"{root}/invoice/discount.py", "content": "X = 1\n"}},
        {"tool": "Read", "input": {"file_path": f"{root}/.cursor/mcp.json"}},
        {"tool": "Shell", "input": {"command": "cat .cursor/mcp.json", "cwd": ""}},
        {"tool": "Write", "input": {"file_path": f"{root}/.cursor/hooks.json", "content": "{}"}},
        {"tool": "Delete", "input": {"file_path": f"{root}/tests/test_totals.py"}},
        {"tool": "Grep", "input": {"pattern": "SECRET", "file_path": f"{root}/.env"}},
        {"tool": "Shell", "input": {"command": "ls invoice", "cwd": "/etc"}},
        {"tool": "Shell", "input": {"command": "python -c 'print(1)'", "cwd": ""}},
        {"tool": "SemanticSearch", "input": {"query": "discount"}},
        {"mcp": "run_check", "input": {"kind": "test"}},
        {"mcp": "finish", "input": {"summary": "fixed", "tests_added": ["tests/test_discount.py"]}},
    ]}])
    assert native_backend(p, cfg, "implementer", box)[0] is p
    res = run_agent(p, cfg, "implementer", "ROLE PROMPT", "do the task", ToolBroker(box, cfg, "implementer", tier=2),
                    charge)
    run = runs()[0]
    assert [(o["tool"], o["decision"]) for o in run["out"]] == [
        ("Write", "allow"), ("Read", "deny"), ("Shell", "deny"), ("Write", "deny"), ("Delete", "deny"),
        ("Grep", "deny"), ("Shell", "deny"), ("Shell", "allow"), ("SemanticSearch", "deny"),
        ("MCP:run_check", "allow"), ("MCP:finish", "allow")]
    assert "PASSED" in run["out"][-2]["text"]
    assert run["listed"] == ["run_check", "finish", "check_result"]
    assert run["cursor_files"] == ["cli.json", "hooks.json", "mcp.json", "rules"]
    assert res.ok and res.summary == "fixed" and res.data["tests_added"] == ["tests/test_discount.py"]
    assert res.turns == 11 and res.cost_usd == pytest.approx((1000 * 1 + 200 * 10) / 1e6)

    a = run["args"]
    assert {"-p", "--force", "--trust", "--approve-mcps"} <= set(a) and a[a.index("--sandbox") + 1] == "enabled"
    assert run["prompt"].startswith("ROLE PROMPT") and "the asf MCP tool finish" in run["prompt"]
    assert run["prompt"].endswith("# Task\ndo the task") and "--resume" not in a
    assert "ANTHROPIC_API_KEY" not in run["env"] and "ASF_BRIDGE_TOKEN" in run["env"]
    # The session's configuration is gone; the project's own .cursor files are as they were.
    assert (box.root / ".cursor/mcp.json").read_text() == '{"mcpServers": {}}'
    assert not (box.root / ".cursor/hooks.json").exists() and not (box.root / ".cursor/cli.json").exists()
    assert (box.root / ".cursor/rules/style.mdc").read_text() == "use cents"
    changed = set(box.changed_files())
    assert "invoice/discount.py" in changed and not {".cursor/hooks.json", ".cursor/cli.json"} & changed


def test_cursor_resume_nudge_leftovers_and_errors(fake, sb):
    cfg, box = sb
    setup, runs = fake
    (box.root / ".cursor").mkdir()
    (box.root / ".cursor/hooks.json").write_text(json.dumps({"hooks": {"preToolUse": [{"command": f"py {BRIDGE}"}]}}))
    p = setup([{"steps": []}, {"steps": [{"mcp": "finish", "input": {"summary": "ok"}}]}])
    res = run_agent(p, cfg, "implementer", "SYS", "task", ToolBroker(box, cfg, "implementer", tier=2), charge,
                    resume="EARLIER")
    first, second = runs()
    assert first["args"][first["args"].index("--resume") + 1] == "EARLIER" and first["prompt"] == "task"
    assert second["args"][second["args"].index("--resume") + 1] == "C-1"
    assert "the asf MCP tool finish" in second["prompt"] and res.ok and res.data["nudged"]
    assert not (box.root / ".cursor").exists()  # a dead session's leftover is not restored

    p = setup([{"is_error": True, "result": "You've hit your usage limit"}])
    with pytest.raises(CursorError) as e:
        run_agent(p, cfg, "tester", "S", "t", ToolBroker(box, cfg, "tester", tier=2), charge)
    assert limits.is_limit_error(str(e.value)) and limits.backend_of_error(str(e.value)) == "cursor"


class SlowBroker:
    def tools(self):
        from asf.broker import _all_specs
        return [s for s in _all_specs(load_cfg(invoice_config())) if s.name in ("run_check", "finish")]

    def call(self, name, args):
        time.sleep(0.6)
        return f"{name} done", False


def test_slow_checks_report_and_can_be_awaited(sb):
    cfg, box = sb
    broker = ToolBroker(box, cfg, "implementer", tier=2)
    bridge = Bridge(broker, NativePolicy(broker, CursorNativeProvider({}).names), check_wait=0.1)
    bridge.broker = SlowBroker()
    try:
        text, err = bridge.call("run_check", {"kind": "test"})
        assert "Still running" in text and not err
        cid = text.split("check ")[1].split(")")[0]
        while "Still running" in text:
            text, err = bridge.call("check_result", {"id": cid})
        assert text == "run_check done"
        assert bridge.call("check_result", {"id": cid})[1]  # already collected
        assert bridge.call("finish", {"summary": "x"}) == ("finish done", False)
    finally:
        bridge.close()
