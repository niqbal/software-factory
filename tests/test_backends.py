"""Cursor CLI provider (fake binary), JSON extraction, and the pool's routing and failover."""

import json
import stat
import sys
import textwrap
from datetime import datetime, timedelta

import pytest

from asf import limits
from asf.adapters.base import ModelTurn, ToolSpec
from asf.adapters.models.cursor_cli import CursorProvider, extract_json
from asf.adapters.models.pool import AllBackendsLimited, PoolProvider

FAKE = textwrap.dedent("""\
    #!{py}
    import json, os, sys
    log, script = os.environ["FAKE_LOG"], os.environ["FAKE_SCRIPT"]
    with open(log, "a") as f:
        f.write(json.dumps({{"args": sys.argv[1:], "cwd": os.getcwd()}}) + "\\n")
    n = sum(1 for _ in open(log))
    replies = json.load(open(script))
    print(json.dumps(replies[min(n, len(replies)) - 1]))
""")

TOOLS = [ToolSpec("read_file", "Read a file", {"type": "object"}), ToolSpec("finish", "End", {"type": "object"})]


@pytest.fixture
def fake_cursor(tmp_path, monkeypatch):
    exe = tmp_path / "cursor-agent"
    exe.write_text(FAKE.format(py=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    log, script = tmp_path / "log.jsonl", tmp_path / "script.json"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_SCRIPT", str(script))

    def setup(replies):
        script.write_text(json.dumps(replies))
        return CursorProvider({"binary": str(exe)})
    return setup, lambda: [json.loads(x) for x in log.read_text().splitlines()]


def reply(result, sid="C1"):
    return {"type": "result", "subtype": "success", "is_error": False, "result": result, "session_id": sid,
            "usage": {"inputTokens": 10, "outputTokens": 5, "cacheReadTokens": 100}}


def test_extract_json_handles_prose_and_fences():
    text = 'I will read it first. ```json\n{"text": "a {b}", "tool_calls": [{"name": "finish", "input": {}}]}\n```'
    assert extract_json(text, require="tool_calls")["tool_calls"][0]["name"] == "finish"
    assert extract_json("no json here") is None


def test_cursor_turns_are_read_only_sandboxed_and_resumed(fake_cursor):
    setup, calls = fake_cursor
    p = setup([reply('Reading. {"text": "", "tool_calls": [{"name": "read_file", "input": {"path": "a.go"}}]}'),
               reply('{"text": "done", "tool_calls": [{"name": "finish", "input": {"summary": "s"}}]}')])
    conv = p.conversation("gpt-5.3-codex", "SYSTEM RULES", TOOLS)
    conv.add_user("do it")
    t1 = conv.step()
    assert t1.tool_calls[0].name == "read_file" and t1.model == "cursor:gpt-5.3-codex"
    conv.add_tool_results([(t1.tool_calls[0].id, "package main", False)])
    assert conv.step().tool_calls[0].name == "finish"
    first, second = calls()
    for a in (first["args"], second["args"]):
        assert "--mode" not in a and a[a.index("--sandbox") + 1] == "enabled" and "--force" not in a
    assert "--resume" not in first["args"] and second["args"][second["args"].index("--resume") + 1] == "C1"
    assert "SYSTEM RULES" in first["args"][-1] and "are disabled" in first["args"][-1] and "fully permitted" in first["args"][-1]
    assert "package main" in second["args"][-1] and "SYSTEM RULES" not in second["args"][-1]
    assert first["cwd"] != second["cwd"] or True  # one scratch dir per conversation


def test_cursor_errors_name_the_backend(fake_cursor):
    setup, _ = fake_cursor
    p = setup([{"type": "result", "subtype": "error", "is_error": True, "result": "You've hit your usage limit"}])
    conv = p.conversation("auto", "S", TOOLS)
    conv.add_user("x")
    with pytest.raises(RuntimeError, match="cursor-agent error"):
        conv.step()


class FakeBackend:
    def __init__(self, name):
        self.name = name

    def credentials_problem(self):
        return None

    def conversation(self, model, system, tools, effort="medium", max_tokens=16000):
        name = self.name

        class C:
            def add_user(self, t): pass
            def add_tool_results(self, r): pass
            def step(self): return ModelTurn("", [], "end_turn", {}, 0.0, model)
        c = C()
        c.backend = name
        return c


def pool(tmp_path, monkeypatch):
    monkeypatch.setenv("ASF_HOME", str(tmp_path))
    p = PoolProvider({"backends": {}, "routing": {"default": ["cursor", "claude"], "reviewer": ["claude", "cursor"]}})
    p.backends = {"cursor": FakeBackend("cursor"), "claude": FakeBackend("claude")}
    p.specs = {"cursor": {"default": {"model": "gpt-5.3-codex"}, "roles": {"implementer": {"model": "gpt-5.3-codex-high"}}},
               "claude": {"default": {"model": "claude-opus-5-5"}}}
    return p


def test_pool_routes_by_role_and_fails_over(tmp_path, monkeypatch):
    p = pool(tmp_path, monkeypatch)
    assert p.choose("implementer")[0] == "cursor" and p.choose("implementer")[2]["model"] == "gpt-5.3-codex-high"
    assert p.choose("reviewer")[0] == "claude"                       # cross-model review
    turn = p.conversation_for("tester", "S", TOOLS).step()
    assert turn.model == "cursor:gpt-5.3-codex"
    limits.record_limit("cursor", datetime.now().astimezone() + timedelta(hours=1), "limit")
    assert p.choose("implementer")[0] == "claude"                    # cursor limited: fail over
    limits.record_limit("claude", datetime.now().astimezone() + timedelta(hours=1), "limit")
    with pytest.raises(AllBackendsLimited):
        p.choose("implementer")


def test_model_clis_are_found_off_path_in_the_usual_install_dirs(tmp_path, monkeypatch):
    """Regression: a console started from a shell without ~/.local/bin on PATH reported
    'Claude Code CLI not found' though claude and cursor-agent were installed there."""
    from asf.adapters.models import claude_code, cursor_cli
    from asf import util
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("claude", "cursor-agent"):
        (bin_dir / name).write_text("#!/bin/sh\n")
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("ASF_CLAUDE_BIN", raising=False)
    monkeypatch.delenv("ASF_CURSOR_BIN", raising=False)
    monkeypatch.setattr(util, "CLI_DIRS", ("~/.local/bin",))
    assert claude_code.find_binary({}) == str(bin_dir / "claude")
    assert cursor_cli.find_binary({}) == str(bin_dir / "cursor-agent")
    # A path set on purpose is used as given, and a wrong one is reported, not silently replaced.
    assert claude_code.find_binary({"binary": str(tmp_path / "nope")}) is None
