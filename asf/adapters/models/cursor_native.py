"""Cursor's agent itself: the implementer, tester and CI fixer run as real `cursor-agent` sessions.

The `cursor_cli` provider points Cursor at an empty folder and has it request tool calls as JSON.
This provider runs the writing roles as ordinary Cursor sessions in the job's worktree instead,
with Cursor's own Read/Write/Grep/Glob/Delete/Shell, its OS sandbox for shell commands, and the
repository's AGENTS.md and rules. What asf keeps is in native.py: policy by Cursor's `preToolUse`
hook, run_check and finish from an asf MCP server, and an audit of what the shell changed.

Cursor reads hooks and MCP servers only from the workspace, so for each session asf writes
`.cursor/hooks.json`, `.cursor/mcp.json` and `.cursor/cli.json` into the checkout and restores
whatever was there afterwards; the agent may neither read nor change them. Cursor gives up on an
MCP call after about a minute, so a longer run_check keeps running and the agent waits for it
with check_result. Cursor has no session budget or turn cap: the wall clock bounds a session,
and spend is estimated from `usd_per_mtok` as with `cursor_cli`.

Settings (factory.yaml `models:`, or a backend in a pool), on top of the `cursor_cli` ones:
    provider: cursor_native
    native_roles: [implementer, tester, ci_fixer]
    session_timeout_s: 3600
    sandbox: enabled            # Cursor's OS sandbox for shell commands (enabled | disabled)
    check_wait_s: 45            # how long one run_check call blocks before reporting "still running"
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
from .cursor_cli import CursorError, CursorProvider, _usage
from .native import BRIDGE, Bridge, NativeSessions, Outcome, ToolNames, tool_notes

CONFIG = (".cursor/hooks.json", ".cursor/mcp.json", ".cursor/cli.json")
AUTH_ENV = ("CURSOR_API_KEY", "LOGNAME")
NAMES = ToolNames(
    reads={"Read": "file_path"},
    writes={"Write": "file_path", "Edit": "file_path", "StrReplace": "file_path", "MultiEdit": "file_path"},
    deletes={"Delete": "file_path"},
    searches={"Grep": ("path", "file_path"), "Glob": ("targetDirectory", "target_directory", "path"),
              "LS": ("path", "target_directory"), "ListDir": ("path", "target_directory")},
    shells={"Shell": ("command", "cwd")},
    mcp_prefix="MCP:",
    free={"TodoWrite", "UpdateTodos", "AwaitShell", "ReadLints", "Task"})
NOTES = tool_notes(
    read="Read, Grep and Glob", write="Write and Delete", shell="Shell",
    run_check="the asf MCP tool run_check", finish="the asf MCP tool finish",
    extra=("\n  A check that runs longer than a minute reports that it is still running, with an id;\n"
           "  call the asf MCP tool check_result with that id to wait for its output."))


class CursorNativeProvider(NativeSessions, CursorProvider):
    kind = "cursor_native"
    names = NAMES
    error_cls = CursorError
    cli_name = "cursor-agent"

    def __init__(self, settings: dict[str, Any], ctx: AdapterContext | None = None):
        super().__init__(settings, ctx)
        self.native_roles = list(self.settings.get("native_roles") or ["implementer", "tester", "ci_fixer"])
        self.session_timeout = int(self.settings.get("session_timeout_s", 3600))
        self.sandbox = str(self.settings.get("sandbox", "enabled"))
        self.check_wait = float(self.settings.get("check_wait_s", 45))

    def hidden_files(self) -> list[str]:
        return list(CONFIG)

    def _launch(self, sb: Any, broker: ToolBroker, bridge: Bridge, tmp: Path) -> Callable[[], None]:
        root = Path(sb.root)
        hook = f"{shlex.quote(sys.executable)} -I {shlex.quote(BRIDGE)} hook cursor"
        ours = {
            CONFIG[0]: json.dumps({"version": 1, "hooks": {"preToolUse": [{"command": hook}]}}),
            # Cursor starts MCP servers with a minimal environment: the bridge's address goes here.
            CONFIG[1]: json.dumps({"mcpServers": {"asf": {
                "command": sys.executable, "args": ["-I", BRIDGE, "mcp"],
                "env": {"ASF_BRIDGE_URL": bridge.url, "ASF_BRIDGE_TOKEN": bridge.token,
                        "ASF_BRIDGE_TOOL_TIMEOUT": str(self.session_timeout)}}}}),
            # Cursor's own permission rules cover searches the hook cannot see into.
            CONFIG[2]: json.dumps({"permissions": {"allow": [], "deny": [f"Read({p})" for p in broker.secret_files]}}),
        }
        saved = {}
        for rel in CONFIG:
            p = root / rel
            if p.is_file():
                text = p.read_text(errors="replace")
                # Left behind by a session whose factory process died: not the project's own file.
                if BRIDGE in text or text == ours[CONFIG[2]]:
                    p.unlink()
                else:
                    saved[rel] = p.read_bytes()
        had_dir = (root / ".cursor").is_dir() and any((root / ".cursor").iterdir())
        (root / ".cursor").mkdir(exist_ok=True)
        for rel, text in ours.items():
            (root / rel).write_text(text)

        def undo() -> None:
            for rel in CONFIG:
                if rel in saved:
                    (root / rel).write_bytes(saved[rel])
                else:
                    (root / rel).unlink(missing_ok=True)
            if not had_dir and (root / ".cursor").is_dir() and not any((root / ".cursor").iterdir()):
                (root / ".cursor").rmdir()
        return undo

    def _command(self, model: str, effort: str, system: str, prompt: str, tmp: Path, max_turns: int | None,
                 resume: str | None, max_usd: float | None) -> tuple[list[str], str]:
        args = [self.binary or "cursor-agent", "-p", "--output-format", "stream-json", "--force", "--trust",
                "--approve-mcps", "--sandbox", self.sandbox]
        if model and model != "auto":
            args += ["--model", model]
        if resume:
            args += ["--resume", resume]
        else:
            # Cursor has no way to add to its own instructions; a new session starts with them.
            prompt = f"{system}{NOTES}\n\n# Task\n{prompt}"
        return args + [prompt], ""

    def _env(self, sb: Any, bridge: Bridge) -> dict[str, str]:
        env = sandbox_env((getattr(sb, "settings", None) or {}).get("env"))
        env.update({k: os.environ[k] for k in AUTH_ENV if k in os.environ})
        env.update({"ASF_BRIDGE_URL": bridge.url, "ASF_BRIDGE_TOKEN": bridge.token})
        return env

    def _tools_in(self, msg: dict[str, Any]) -> list[str]:
        if msg.get("type") != "tool_call" or msg.get("subtype") != "started":
            return []
        return [k.removesuffix("ToolCall") for k in (msg.get("tool_call") or {}) if k.endswith("ToolCall")]

    def _outcome(self, msg: dict[str, Any], model: str, tools: int) -> Outcome:
        usage = _usage(msg)
        subtype = str(msg.get("subtype") or "")
        failed = bool(msg.get("is_error")) or subtype not in ("", "success")
        turn = ModelTurn(text=str(msg.get("result") or "")[:2000], tool_calls=[], stop_reason=subtype or "success",
                         usage=usage, cost_usd=self.cost(usage), model=f"cursor:{model}")
        return Outcome(session=msg.get("session_id"), turn=turn, turns=tools, completed=not failed,
                       error=f"cursor-agent error: {str(msg.get('result') or subtype)[:300]}" if failed else "")

    def _mcp_name(self, tool: str) -> str:
        return f"the asf MCP tool {tool}"
