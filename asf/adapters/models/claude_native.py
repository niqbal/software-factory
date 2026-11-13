"""Claude Code as the agent itself: the implementer, tester and CI fixer run as real sessions.

The `claude_code` provider switches Claude Code's tools off and emulates tool calls through
structured output. This provider runs the writing roles as ordinary Claude Code sessions in the
job's worktree instead (see native.py for what asf keeps: policy by hook, run_check and finish
over MCP, an audit of shell changes): Claude Code's own system prompt with the role prompt
appended, its Read/Edit/Write/Glob/Grep/Bash, subagents and skills, the repository's CLAUDE.md
files, and the job's remaining budget enforced inside the session (--max-budget-usd).

Reviewers, security review and triage use the inherited `claude_code` path (no tools to grant).

Settings (factory.yaml, under `models:`), on top of the `claude_code` ones:
    provider: claude_code_native
    native_roles: [implementer, tester, ci_fixer]
    session_max_turns: 150      # per session (models.max_turns.<role> is for the other providers)
    session_timeout_s: 3600     # wall clock per session (the job's minute budget also applies)
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Callable

from ...broker import ToolBroker
from ...util import sandbox_env
from ..base import AdapterContext, ModelTurn
from .claude_code import ClaudeCodeError, ClaudeCodeProvider, _usage
from .native import BRIDGE, Bridge, NativeSessions, Outcome, ToolNames, tool_notes

NATIVE_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash", "Task", "Skill"]
AUTH_ENV = ("CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "LOGNAME")
STOPS = {"error_max_turns", "error_max_budget_usd"}  # the session's own limits: a stop, not a failure
NAMES = ToolNames(
    reads={"Read": "file_path"},
    writes={"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"},
    searches={"Glob": ("path",), "Grep": ("path",)},
    shells={"Bash": ("command", "")},
    mcp_prefix="mcp__asf__",
    free={"Task", "Skill", "TodoWrite", "ToolSearch"})
NOTES = tool_notes(read="Glob, Read, Grep", write="Write, Edit, and `rm` in Bash", shell="Bash",
                   run_check="mcp__asf__run_check", finish="mcp__asf__finish")


class ClaudeNativeProvider(NativeSessions, ClaudeCodeProvider):
    kind = "claude_code_native"
    names = NAMES
    error_cls = ClaudeCodeError
    cli_name = "claude -p"

    def __init__(self, settings: dict[str, Any], ctx: AdapterContext | None = None):
        super().__init__(settings, ctx)
        self.native_roles = list(self.settings.get("native_roles") or ["implementer", "tester", "ci_fixer"])
        self.max_turns = int(self.settings.get("session_max_turns", 150))
        self.session_timeout = int(self.settings.get("session_timeout_s", 3600))

    def _launch(self, sb: Any, broker: ToolBroker, bridge: Bridge, tmp: Path) -> Callable[[], None]:
        # By file path, isolated (-I): the bridge is stdlib only, and nothing in the checkout or
        # the environment can change what it imports.
        hook = f"{shlex.quote(sys.executable)} -I {shlex.quote(BRIDGE)} hook"
        (tmp / "settings.json").write_text(json.dumps({
            "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": hook}]}]},
            # Claude Code applies Read rules to Grep and Glob as well, which the hook cannot see into.
            "permissions": {"deny": ["WebFetch", "WebSearch"] + [f"Read({p})" for p in broker.secret_files]}}))
        (tmp / "mcp.json").write_text(json.dumps({"mcpServers": {"asf": {
            "command": sys.executable, "args": ["-I", BRIDGE, "mcp"]}}}))
        return lambda: None

    def _command(self, model: str, effort: str, system: str, prompt: str, tmp: Path, max_turns: int | None,
                 resume: str | None, max_usd: float | None) -> tuple[list[str], str]:
        args = [self.binary or "claude", "-p", "--output-format", "stream-json", "--verbose",
                "--append-system-prompt", system + NOTES,
                "--tools", ",".join(NATIVE_TOOLS),
                "--allowedTools", ",".join(NATIVE_TOOLS + ["mcp__asf__run_check", "mcp__asf__finish"]),
                "--permission-mode", "dontAsk", "--permission-prompts", "none",
                "--settings", str(tmp / "settings.json"), "--setting-sources", "project",
                "--strict-mcp-config", "--mcp-config", str(tmp / "mcp.json"),
                "--model", model, "--max-turns", str(max_turns or self.max_turns)]
        if effort:
            args += ["--effort", effort]
        if resume:
            args += ["--resume", resume]
        if max_usd is not None:
            args += ["--max-budget-usd", f"{max(max_usd, 0.01):.2f}"]
        return args, prompt

    def _env(self, sb: Any, bridge: Bridge) -> dict[str, str]:
        env = sandbox_env((getattr(sb, "settings", None) or {}).get("env"))
        env.update({k: os.environ[k] for k in AUTH_ENV if k in os.environ})
        ms = str(int(self.timeout * 1000))
        env.update({"ASF_BRIDGE_URL": bridge.url, "ASF_BRIDGE_TOKEN": bridge.token,
                    "ASF_BRIDGE_TOOL_TIMEOUT": str(self.session_timeout),
                    "MCP_TOOL_TIMEOUT": str(self.session_timeout * 1000),
                    "BASH_DEFAULT_TIMEOUT_MS": ms, "BASH_MAX_TIMEOUT_MS": ms,
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
        return env

    def _tools_in(self, msg: dict[str, Any]) -> list[str]:
        if msg.get("type") != "assistant":
            return []
        return [b.get("name", "") for b in (msg.get("message") or {}).get("content") or []
                if isinstance(b, dict) and b.get("type") == "tool_use"]

    def _outcome(self, msg: dict[str, Any], model: str, tools: int) -> Outcome:
        subtype = str(msg.get("subtype") or "")
        turn = ModelTurn(text=str(msg.get("result") or "")[:2000], tool_calls=[], stop_reason=subtype,
                         usage=_usage(msg), cost_usd=float(msg.get("total_cost_usd") or 0),
                         model=next(iter(msg.get("modelUsage") or {}), model))
        error = ""
        if msg.get("is_error") and subtype not in STOPS:
            error = f"claude -p error: {str(msg.get('result') or subtype)[:300]}"
        return Outcome(session=msg.get("session_id"), turn=turn, turns=int(msg.get("num_turns") or 0),
                       completed=subtype == "success" and not msg.get("is_error"), error=error)

    def _mcp_name(self, tool: str) -> str:
        return f"mcp__asf__{tool}"
