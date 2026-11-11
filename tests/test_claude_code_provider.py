"""Claude Code headless provider, driven by a fake `claude` executable (no network)."""
import json
import os
import stat
import sys
import textwrap

import pytest

from asf.adapters.base import ToolSpec
from asf.adapters.models.claude_code import ClaudeCodeError, ClaudeCodeProvider

FAKE = textwrap.dedent('''\
    #!{py}
    import json, os, sys
    args = sys.argv[1:]
    log = os.environ["FAKE_LOG"]
    prompt = sys.stdin.read()
    with open(log, "a") as f:
        f.write(json.dumps({{"args": args, "prompt": prompt}}) + "\\n")
    n = sum(1 for _ in open(log))
    script = json.load(open(os.environ["FAKE_SCRIPT"]))
    reply = script[min(n, len(script)) - 1]
    print(json.dumps(reply))
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

    def setup(replies):
        script.write_text(json.dumps(replies))
        return ClaudeCodeProvider({"binary": str(exe)})

    def calls():
        return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    return setup, calls


def reply(tool_calls, text="", sid="S1", cost=0.01):
    return {"session_id": sid, "total_cost_usd": cost, "structured_output": {"text": text, "tool_calls": tool_calls},
            "usage": {"input_tokens": 3, "output_tokens": 5, "cache_read_input_tokens": 100,
                      "cache_creation_input_tokens": 0}, "modelUsage": {"claude-sonnet-5-5": {}}}


TOOLS = [ToolSpec("read_file", "Read a file", {"type": "object", "properties": {"path": {"type": "string"}}}),
         ToolSpec("finish", "End the task", {"type": "object"})]


def test_turns_resume_session_and_carry_tool_results(fake):
    setup, calls = fake
    p = setup([reply([{"name": "read_file", "input": {"path": "a.go"}}]),
               reply([{"name": "finish", "input": {"summary": "done"}}], text="ok")])
    conv = p.conversation("claude-sonnet-5-5", "SYSTEM", TOOLS, "high")
    conv.add_user("do the task")
    t1 = conv.step()
    assert [c.name for c in t1.tool_calls] == ["read_file"] and t1.stop_reason == "tool_use"
    assert t1.cost_usd == pytest.approx(0.01) and t1.usage["cache_read"] == 100
    conv.add_tool_results([(t1.tool_calls[0].id, "package main", False)])
    t2 = conv.step()
    assert t2.tool_calls[0].name == "finish" and t2.tool_calls[0].id != t1.tool_calls[0].id
    first, second = calls()
    assert "--resume" not in first["args"] and first["prompt"] == "do the task"
    assert second["args"][second["args"].index("--resume") + 1] == "S1"
    assert 'id="call_1"' in second["prompt"] and "package main" in second["prompt"]
    a = first["args"]
    assert a[a.index("--tools") + 1] == "" and a[a.index("--effort") + 1] == "high"
    sysprompt = a[a.index("--system-prompt") + 1]
    assert sysprompt.startswith("SYSTEM") and "read_file" in sysprompt and "Tool protocol" in sysprompt


def test_api_key_is_not_passed_to_the_cli(fake, tmp_path, monkeypatch):
    setup, calls = fake
    exe_env = tmp_path / "env.txt"
    p = setup([reply([])])
    # Wrap the fake to record its environment.
    orig = p.binary
    wrapper = tmp_path / "wrap"
    wrapper.write_text(f"#!/bin/sh\nenv > {exe_env}\nexec '{orig}' \"$@\"\n")
    wrapper.chmod(0o755)
    p.binary = str(wrapper)
    conv = p.conversation("m", "S", TOOLS, "low")
    conv.add_user("x")
    conv.step()
    assert "sk-should-not-leak" not in exe_env.read_text()


def test_retries_on_missing_structured_output_then_fails(fake):
    setup, calls = fake
    bad = {"session_id": "S1", "total_cost_usd": 0.0, "result": "plain text"}
    p = setup([bad, reply([{"name": "finish", "input": {}}])])
    conv = p.conversation("m", "S", TOOLS, "low")
    conv.add_user("x")
    assert conv.step().tool_calls[0].name == "finish"
    assert "not a valid JSON" in calls()[1]["prompt"]

    p2 = setup([bad, bad, bad, bad])
    conv2 = p2.conversation("m", "S", TOOLS, "low")
    conv2.add_user("x")
    with pytest.raises(ClaudeCodeError):
        conv2.step()


def test_structured_and_credentials(fake, tmp_path):
    setup, calls = fake
    p = setup([{"session_id": "S", "total_cost_usd": 0.002, "structured_output": {"tier": 1}}])
    out, turn = p.structured("m", "S", "classify", {"type": "object"})
    assert out == {"tier": 1} and turn.cost_usd == pytest.approx(0.002)
    assert "--no-session-persistence" in calls()[-1]["args"]
    assert p.credentials_problem() is None
    assert ClaudeCodeProvider({"binary": str(tmp_path / "missing")}).credentials_problem()
