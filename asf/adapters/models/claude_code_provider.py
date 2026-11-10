"""Claude Code provider: run each agent through the ``claude`` CLI instead of the API.

Claude Code runs its own agent loop, so the factory cannot hand it one turn at a
time. Instead each agent is a headless ``claude -p`` process with every built-in
tool switched off, and the only tools it can call are the broker's, served by an
in-process MCP server (see ``mcp_bridge``). The broker's checks, redaction and
audit log therefore apply exactly as they do with the API provider.

Isolation from the developer's own Claude Code setup:

- ``--tools ""`` removes Read, Edit, Bash and every other built-in tool.
- ``--strict-mcp-config`` loads no MCP server except the factory's.
- ``--allowedTools`` lists only the role's broker tools and ``--permission-mode
  dontAsk`` denies anything else instead of prompting.
- ``--disable-slash-commands`` turns off skills and commands.
- ``--setting-sources ""`` skips user, project and local settings, including hooks.
- The process runs in an empty temporary directory, never in the sandbox, so no
  repository CLAUDE.md is loaded and relative paths cannot reach the sandbox.

Authentication follows ``models.claude_code.auth``:

- ``login`` (default) uses whatever the CLI is signed in with, such as a Claude
  subscription. ``ANTHROPIC_API_KEY`` is removed from the child's environment so
  the login is what gets used.
- ``api_key`` passes the factory's API key and adds ``--bare``, which also skips
  auto-memory and CLAUDE.md discovery entirely.

Cost: the CLI reports ``total_cost_usd`` at API list prices. With ``login`` auth
that is an estimate of API-equivalent spend, not money charged; budgets still use
it so a runaway agent is stopped the same way.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from ..base import AdapterContext, AgentRun, AgentSession, Conversation, ModelProvider, ModelTurn, ToolSpec
from .mcp_bridge import BrokerMCPServer

AUTH_MODES = ("login", "api_key")
STOPS_THAT_RETURN = {"success", "error_max_turns", "error_max_budget_usd"}


class ClaudeCodeError(RuntimeError):
    pass


def find_binary(settings: dict[str, Any]) -> str | None:
    """Configured path, then ASF_CLAUDE_BIN, then ``claude`` on PATH."""
    for candidate in (settings.get("binary"), os.environ.get("ASF_CLAUDE_BIN")):
        if candidate:
            path = Path(candidate).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
            found = shutil.which(str(candidate))
            if found:
                return found
    return shutil.which("claude")


def _usage(u: dict[str, Any] | None) -> dict[str, int]:
    u = u or {}
    return {"input": int(u.get("input_tokens") or 0),
            "output": int(u.get("output_tokens") or 0),
            "cache_read": int(u.get("cache_read_input_tokens") or 0),
            "cache_write": int(u.get("cache_creation_input_tokens") or 0)}


def parse_result(stdout: str) -> dict[str, Any] | None:
    """The CLI prints one JSON result object; tolerate stray lines before it."""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            return obj
    return None


class ClaudeCodeProvider(ModelProvider):
    kind = "claude_code"
    owns_loop = True

    def __init__(self, settings: dict[str, Any], ctx: AdapterContext | None = None):
        super().__init__(settings, ctx)
        self.cc: dict[str, Any] = dict(self.settings.get("claude_code") or {})
        self.auth = self.cc.get("auth", "login")
        if self.auth not in AUTH_MODES:
            raise ValueError(f"models.claude_code.auth must be one of {AUTH_MODES}, got {self.auth!r}")
        self.timeout = float(self.cc.get("timeout_s", 3600))
        self.extra_args = [str(a) for a in self.cc.get("extra_args", [])]
        self.binary = find_binary(self.cc)
        # Seam for tests: replaces subprocess.run with the same signature.
        self.runner: Callable[..., subprocess.CompletedProcess] = subprocess.run

    # ------------------------------------------------------------ process --
    def _require_binary(self) -> str:
        if not self.binary:
            raise ClaudeCodeError("claude CLI not found: install Claude Code, or set "
                                  "models.claude_code.binary (or ASF_CLAUDE_BIN) to its path")
        return self.binary

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.auth == "login":
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
        else:
            key_env = self.settings.get("api_key_env", "ANTHROPIC_API_KEY")
            key = self.ctx.secrets(key_env) if self.ctx else os.environ.get(key_env)
            if not key:
                raise ClaudeCodeError(f"models.claude_code.auth is api_key but {key_env} is not set")
            env["ANTHROPIC_API_KEY"] = key
        return env

    def base_args(self, model: str, effort: str) -> list[str]:
        args = [self._require_binary(), "-p", "--output-format", "json",
                "--model", model, "--effort", effort,
                "--tools", "", "--strict-mcp-config", "--disable-slash-commands",
                "--permission-mode", "dontAsk"]
        if self.auth == "api_key":
            args.append("--bare")
        else:
            args += ["--setting-sources", ""]
        return args + self.extra_args

    def invoke(self, args: list[str], prompt: str, cwd: str) -> dict[str, Any]:
        try:
            proc = self.runner(args, input=prompt, capture_output=True, text=True, cwd=cwd,
                               env=self.env(), timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            raise ClaudeCodeError(f"claude did not finish within {self.timeout:.0f}s") from e
        result = parse_result(proc.stdout or "")
        if result is None:
            tail = (proc.stderr or proc.stdout or "").strip()[-1500:]
            raise ClaudeCodeError(f"claude exited {proc.returncode} without a result: {tail or '(no output)'}")
        subtype = result.get("subtype", "")
        if subtype not in STOPS_THAT_RETURN or (subtype == "success" and result.get("is_error")):
            raise ClaudeCodeError(f"claude stopped with {subtype or 'an error'}: "
                                  f"{str(result.get('result') or result.get('errors') or '')[:1500]}")
        return result

    @staticmethod
    def to_turn(result: dict[str, Any], model: str) -> ModelTurn:
        used = list((result.get("modelUsage") or {}).keys())
        return ModelTurn(text=str(result.get("result") or ""), tool_calls=[],
                         stop_reason=result.get("subtype", ""), usage=_usage(result.get("usage")),
                         cost_usd=float(result.get("total_cost_usd") or 0.0),
                         model=used[0] if len(used) == 1 else model)

    # ------------------------------------------------------- provider API --
    def conversation(self, model: str, system: str, tools: list[ToolSpec],
                     effort: str = "medium", max_tokens: int = 16000) -> Conversation:
        raise NotImplementedError("claude_code runs its own agent loop; use agent_session()")

    def agent_session(self, model: str, system: str, tools: list[ToolSpec],
                      call_tool: Callable[[str, dict[str, Any]], tuple[str, bool]],
                      effort: str = "medium") -> AgentSession:
        return ClaudeCodeSession(self, model, system, tools, call_tool, effort)

    def structured(self, model: str, system: str, prompt: str, schema: dict[str, Any],
                   effort: str = "low") -> tuple[dict[str, Any], ModelTurn]:
        with tempfile.TemporaryDirectory(prefix="asf-claude-") as cwd:
            system_file = Path(cwd) / "system.md"
            system_file.write_text(system)
            args = self.base_args(model, effort) + [
                "--system-prompt-file", str(system_file), "--json-schema", json.dumps(schema),
                "--no-session-persistence", "--max-turns", str(self.cc.get("structured_max_turns", 4))]
            result = self.invoke(args, prompt, cwd)
        data = result.get("structured_output")
        if not isinstance(data, dict):
            try:
                data = json.loads(str(result.get("result") or ""))
            except ValueError as e:
                raise ClaudeCodeError("claude returned no structured output") from e
        return data, self.to_turn(result, model)


class ClaudeCodeSession(AgentSession):
    """One agent: a loopback MCP server for its broker plus a resumable claude session."""

    def __init__(self, provider: ClaudeCodeProvider, model: str, system: str, tools: list[ToolSpec],
                 call_tool: Callable[[str, dict[str, Any]], tuple[str, bool]], effort: str):
        self.p, self.model, self.effort = provider, model, effort
        self.server = BrokerMCPServer(tools, call_tool).start()
        self._tmp = tempfile.TemporaryDirectory(prefix="asf-claude-")
        self.cwd = self._tmp.name
        self.system_file = Path(self.cwd) / "system.md"
        self.system_file.write_text(system)
        self.mcp_file = Path(self.cwd) / "mcp.json"
        self.mcp_file.write_text(json.dumps(self.server.mcp_config()))
        os.chmod(self.mcp_file, 0o600)  # holds the bearer token
        self.session_id = ""

    def args(self, max_turns: int, budget_usd: float | None) -> list[str]:
        args = self.p.base_args(self.model, self.effort) + [
            "--mcp-config", str(self.mcp_file),
            "--allowedTools", ",".join(self.server.qualified_names()),
            "--system-prompt-file", str(self.system_file),
            "--max-turns", str(max(1, max_turns))]
        if budget_usd is not None:
            args += ["--max-budget-usd", f"{max(budget_usd, 0.01):.2f}"]
        if self.session_id:
            args += ["--resume", self.session_id]
        return args

    def send(self, prompt: str, max_turns: int, budget_usd: float | None = None) -> AgentRun:
        result = self.p.invoke(self.args(max_turns, budget_usd), prompt, self.cwd)
        self.session_id = result.get("session_id") or self.session_id
        return AgentRun(turn=self.p.to_turn(result, self.model), turns=int(result.get("num_turns") or 0),
                        stop=result.get("subtype", ""), session_id=self.session_id)

    def close(self) -> None:
        self.server.stop()
        self._tmp.cleanup()
