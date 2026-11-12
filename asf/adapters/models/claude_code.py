"""Claude Code headless provider: model turns come from `claude -p` instead of the API.

The broker must stay the only way an agent touches the sandbox, so Claude Code's own tools
are switched off (`--tools ""`). Each turn is one headless call whose structured output
(`--json-schema`) carries the tool calls the model wants. The agent loop in asf runs them
through the broker, exactly as with the API provider, and sends the results back in the
next call, which resumes the same Claude Code session (`--resume`), so the conversation and
its prompt cache live in Claude Code.

Settings (factory.yaml, under `models:`):
    provider: claude_code
    binary: /path/to/claude      # or env ASF_CLAUDE_BIN, or `claude` on PATH
    timeout_s: 600               # per turn
    retries: 2                   # re-ask when the reply is not valid structured output

Costs are Claude Code's own `total_cost_usd` per call (list price; on a subscription this
is not what is billed, but budgets still use it as the measure of work).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import Any

from ...util import find_cli
from ..base import AdapterContext, Conversation, ModelProvider, ModelTurn, ToolCall, ToolSpec

TURN_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "Brief reasoning or the final message"},
        "tool_calls": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"name": {"type": "string"}, "input": {"type": "object"}},
                      "required": ["name", "input"]},
        },
    },
    "required": ["text", "tool_calls"],
}

PROTOCOL = """

# Tool protocol (read carefully)
You have no built-in tools. A host program executes tools for you. Every reply you give is
one JSON object with two fields:
- "text": brief reasoning, or your final message
- "tool_calls": the tools the host should run now, each {"name": ..., "input": {...}}
After your reply the host runs the tool calls in order and sends their results in the next
message as <tool_result id=... error=true|false> blocks. Requesting a tool in tool_calls IS
how you use it; never say a tool is unavailable. Request several independent calls in one
reply when you can. When the task is done, call the finishing tool named in your
instructions.

# Available tools (name: description; input JSON schema)
"""


class ClaudeCodeError(RuntimeError):
    pass


def find_binary(settings: dict[str, Any]) -> str | None:
    return find_cli("claude", settings.get("binary"), "ASF_CLAUDE_BIN")


def _usage(d: dict[str, Any]) -> dict[str, int]:
    u = d.get("usage") or {}
    return {"input": u.get("input_tokens", 0) or 0, "output": u.get("output_tokens", 0) or 0,
            "cache_read": u.get("cache_read_input_tokens", 0) or 0,
            "cache_write": u.get("cache_creation_input_tokens", 0) or 0}


class ClaudeCodeConversation(Conversation):
    def __init__(self, provider: "ClaudeCodeProvider", model: str, system: str,
                 tools: list[ToolSpec], effort: str):
        self.p, self.model, self.effort = provider, model, effort
        specs = "\n".join(f"- {t.name}: {t.description}\n  schema: {json.dumps(t.schema)}" for t in tools)
        self.system = system + PROTOCOL + specs
        self.session: str | None = None
        self.pending: list[str] = []
        self.n = 0
        # Claude Code keys sessions by working directory: keep one quiet dir per conversation.
        self.cwd = tempfile.mkdtemp(prefix="asf-cc-")

    def add_user(self, text: str) -> None:
        self.pending.append(text)

    def add_tool_results(self, results: list[tuple[str, str, bool]]) -> None:
        for tid, text, err in results:
            self.pending.append(f'<tool_result id="{tid}" error="{str(err).lower()}">\n{text or "(empty)"}\n</tool_result>')

    def step(self) -> ModelTurn:
        prompt = "\n\n".join(self.pending) or "Continue."
        self.pending = []
        cost, usage = 0.0, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        last_err = ""
        for attempt in range(self.p.retries + 1):
            d = self.p.call(prompt, self.system, self.model, self.effort, TURN_SCHEMA,
                            resume=self.session, cwd=self.cwd)
            self.session = d.get("session_id") or self.session
            cost += float(d.get("total_cost_usd") or 0)
            for k, v in _usage(d).items():
                usage[k] += v
            out = d.get("structured_output")
            if isinstance(out, dict) and isinstance(out.get("tool_calls"), list):
                calls = []
                for c in out["tool_calls"]:
                    self.n += 1
                    inp = c.get("input") if isinstance(c.get("input"), dict) else {}
                    calls.append(ToolCall(f"call_{self.n}", str(c.get("name", "")), inp))
                model = next(iter(d.get("modelUsage") or {}), self.model)
                return ModelTurn(text=str(out.get("text", "")), tool_calls=calls,
                                 stop_reason="tool_use" if calls else "end_turn",
                                 usage=usage, cost_usd=cost, model=model)
            last_err = str(d.get("result") or d.get("subtype") or "no structured output")[:300]
            prompt = ("Your last reply was not a valid JSON object with \"text\" and \"tool_calls\". "
                      "Reply again in that format.")
        raise ClaudeCodeError(f"no valid structured reply after {self.p.retries + 1} tries: {last_err}")


class ClaudeCodeProvider(ModelProvider):
    kind = "claude_code"

    def __init__(self, settings: dict[str, Any], ctx: AdapterContext | None = None):
        super().__init__(settings, ctx)
        self.binary = find_binary(self.settings)
        self.timeout = int(self.settings.get("timeout_s", 600))
        self.retries = int(self.settings.get("retries", 2))

    def credentials_problem(self) -> str | None:
        if not self.binary:
            return ("Claude Code CLI not found: set models.binary in factory.yaml, export "
                    "ASF_CLAUDE_BIN, or put `claude` on PATH")
        return None

    def call(self, prompt: str, system: str, model: str, effort: str, schema: dict[str, Any],
             resume: str | None = None, cwd: str | None = None, persist: bool = True) -> dict[str, Any]:
        if not self.binary:
            raise ClaudeCodeError(self.credentials_problem() or "no binary")
        args = [self.binary, "-p", "--output-format", "json", "--tools", "",
                "--json-schema", json.dumps(schema), "--system-prompt", system,
                "--model", model, "--setting-sources", "", "--strict-mcp-config"]
        if effort:
            args += ["--effort", effort]
        if resume:
            args += ["--resume", resume]
        if not persist:
            args += ["--no-session-persistence"]
        # Scrub API keys so the CLI uses its own sign-in, never a key meant for something else.
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        try:
            r = subprocess.run(args, input=prompt, capture_output=True, text=True,
                               timeout=self.timeout, cwd=cwd or tempfile.gettempdir(), env=env)
        except subprocess.TimeoutExpired as e:
            raise ClaudeCodeError(f"claude -p timed out after {self.timeout}s") from e
        try:
            d = json.loads(r.stdout)
        except json.JSONDecodeError as e:
            raise ClaudeCodeError(f"claude -p returned non-JSON (exit {r.returncode}): "
                                  f"{(r.stderr or r.stdout)[:300]}") from e
        if d.get("is_error") and d.get("structured_output") is None:
            raise ClaudeCodeError(f"claude -p error: {str(d.get('result'))[:300]}")
        return d

    def conversation(self, model: str, system: str, tools: list[ToolSpec],
                     effort: str = "medium", max_tokens: int = 16000) -> Conversation:
        return ClaudeCodeConversation(self, model, system, tools, effort)

    def structured(self, model: str, system: str, prompt: str, schema: dict[str, Any],
                   effort: str = "low") -> tuple[dict[str, Any], ModelTurn]:
        d = self.call(prompt, system, model, effort, schema, persist=False)
        out = d.get("structured_output")
        if not isinstance(out, dict):
            raise ClaudeCodeError(f"no structured output: {str(d.get('result'))[:300]}")
        turn = ModelTurn(text=json.dumps(out), tool_calls=[], stop_reason="end_turn", usage=_usage(d),
                         cost_usd=float(d.get("total_cost_usd") or 0),
                         model=next(iter(d.get("modelUsage") or {}), model))
        return out, turn
