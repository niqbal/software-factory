"""Cursor CLI headless provider: model turns come from `cursor-agent -p`.

Cursor's headless mode always has its own tools (read, search, shell, write), with no switch to
turn them off and no structured-output mode. The broker must stay the only way an agent touches
the repository, so every call runs:

  --sandbox enabled    OS sandbox
  cwd = an empty scratch folder whose .cursor/cli.json denies Read, Write, Shell and MCP, so
        Cursor's own tools fail and have nothing to touch
(Read-only `--mode ask` was tried and dropped: the model then refused to request the host's
write tools as well, believing all edits were forbidden.)

The model is told its built-in tools see only that empty folder, and it requests real work as
JSON tool calls that asf runs through the broker, exactly as with the Claude Code provider.
Later turns resume the same Cursor chat (`--resume <session_id>`).

Settings (factory.yaml `models:` for a single-provider project, or a backend in a pool):
    provider: cursor_cli
    binary: /path/to/cursor-agent     # or env ASF_CURSOR_BIN, or cursor-agent on PATH
    timeout_s: 900
    retries: 2
    usd_per_mtok: {in: 0, out: 0}      # optional cost estimate; Cursor bills against its own balance
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import Any

from ..base import AdapterContext, Conversation, ModelProvider, ModelTurn, ToolCall, ToolSpec

PREAMBLE = """You are an agent controlled by a host program (a software factory).

IMPORTANT: your built-in tools (file read, search, shell, edit) are disabled and only see an
empty scratch folder. Do not use them. The real project is reachable ONLY through the host
tools listed below, and those are fully permitted: when the task needs code changed, tests
written or commands run, request the host's write/edit/run tools. Any read-only setting of
your own client does not apply to host tools.

To use a host tool, put it in "tool_calls" in your reply; the host runs the calls in order and
sends the results in the next message as <tool_result id=... error=true|false> blocks.
Requesting a tool IS how you use it; never say a tool is unavailable. Request several
independent calls in one reply when you can. When the task is done, call the finishing tool
named in your instructions.

Every reply is ONLY one JSON object, with no prose before or after it and no code fence:
{"text": "<brief reasoning or final message>", "tool_calls": [{"name": "<tool>", "input": {...}}]}
"""

REMINDER = ("\n\nRemember: do not use your built-in tools; the project is reachable only through the host "
            "tools, requested in tool_calls. Reply with ONLY the JSON object.")

# Workspace permissions that make Cursor's own tools fail instead of quietly seeing an empty folder.
DENY_ALL = {"permissions": {"allow": [], "deny": ["Read(**)", "Write(**)", "Shell(*)", "Mcp(*)"]}}


def scratch_workspace() -> str:
    d = tempfile.mkdtemp(prefix="asf-cursor-")
    os.makedirs(os.path.join(d, ".cursor"), exist_ok=True)
    with open(os.path.join(d, ".cursor", "cli.json"), "w") as f:
        json.dump(DENY_ALL, f)
    return d


class CursorError(RuntimeError):
    pass


def find_binary(settings: dict[str, Any]) -> str | None:
    cand = settings.get("binary") or os.environ.get("ASF_CURSOR_BIN") or shutil.which("cursor-agent")
    return cand if cand and os.path.isfile(cand) and os.access(cand, os.X_OK) else None


def extract_json(text: str, require: str | None = None) -> dict[str, Any] | None:
    """The last top-level JSON object in text (models sometimes wrap it in prose or fences)."""
    best = None
    depth, start, in_str, esc = 0, -1, False, False
    for i, ch in enumerate(text or ""):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    obj = json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and (require is None or require in obj):
                    best = obj
    return best


def _usage(d: dict[str, Any]) -> dict[str, int]:
    u = d.get("usage") or {}
    return {"input": u.get("inputTokens", 0) or 0, "output": u.get("outputTokens", 0) or 0,
            "cache_read": u.get("cacheReadTokens", 0) or 0, "cache_write": u.get("cacheWriteTokens", 0) or 0}


class CursorConversation(Conversation):
    def __init__(self, provider: "CursorProvider", model: str, system: str, tools: list[ToolSpec]):
        self.p, self.model = provider, model
        specs = "\n".join(f"- {t.name}: {t.description}\n  schema: {json.dumps(t.schema)}" for t in tools)
        self.preamble = f"{PREAMBLE}\n# Host tools (name: description; input JSON schema)\n{specs}\n\n# Your instructions\n{system}"
        self.session: str | None = None
        self.pending: list[str] = []
        self.n = 0
        self.cwd = scratch_workspace()

    def add_user(self, text: str) -> None:
        self.pending.append(text)

    def add_tool_results(self, results: list[tuple[str, str, bool]]) -> None:
        for tid, text, err in results:
            self.pending.append(f'<tool_result id="{tid}" error="{str(err).lower()}">\n{text or "(empty)"}\n</tool_result>')

    def step(self) -> ModelTurn:
        body = "\n\n".join(self.pending) or "Continue."
        self.pending = []
        prompt = (self.preamble + "\n\n# Task\n" + body if self.session is None else body) + REMINDER
        usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        cost, last = 0.0, ""
        for _ in range(self.p.retries + 1):
            d = self.p.call(prompt, self.model, resume=self.session, cwd=self.cwd)
            self.session = d.get("session_id") or self.session
            for k, v in _usage(d).items():
                usage[k] += v
            cost += self.p.cost(_usage(d))
            out = extract_json(d.get("result", ""), require="tool_calls")
            names = [c.get("name") for c in (out or {}).get("tool_calls") or [] if isinstance(c, dict)]
            if out is not None and self.n == 0 and all(n in ("finish", "submit_review") for n in names):
                # Nothing requested before any host tool was used: it tried its own tools instead.
                last = str(out.get("text", ""))[:300]
                prompt = ("You requested no host tools. Your built-in tools cannot see the project; the host "
                          "tools in tool_calls are the only way to read or change it. Request the host tools "
                          "you need now, as the JSON object.")
                continue
            if out is not None and isinstance(out.get("tool_calls"), list):
                calls = []
                for c in out["tool_calls"]:
                    if not isinstance(c, dict):
                        continue
                    self.n += 1
                    inp = c.get("input") if isinstance(c.get("input"), dict) else {}
                    calls.append(ToolCall(f"call_{self.n}", str(c.get("name", "")), inp))
                return ModelTurn(text=str(out.get("text", "")), tool_calls=calls,
                                 stop_reason="tool_use" if calls else "end_turn", usage=usage, cost_usd=cost,
                                 model=f"cursor:{self.model}")
            last = str(d.get("result", ""))[:300]
            prompt = ('Your last reply was not a single JSON object with "text" and "tool_calls". '
                      "Reply again with ONLY that JSON object.")
        raise CursorError(f"cursor-agent gave no valid JSON reply after {self.p.retries + 1} tries: {last}")


class CursorProvider(ModelProvider):
    kind = "cursor_cli"

    def __init__(self, settings: dict[str, Any], ctx: AdapterContext | None = None):
        super().__init__(settings, ctx)
        self.binary = find_binary(self.settings)
        self.timeout = int(self.settings.get("timeout_s", 900))
        self.retries = int(self.settings.get("retries", 2))
        self.price = self.settings.get("usd_per_mtok") or {}

    def credentials_problem(self) -> str | None:
        if not self.binary:
            return "Cursor CLI not found: set binary, export ASF_CURSOR_BIN, or put cursor-agent on PATH"
        return None

    def cost(self, usage: dict[str, int]) -> float:
        return (usage.get("input", 0) * float(self.price.get("in", 0))
                + usage.get("output", 0) * float(self.price.get("out", 0))) / 1_000_000

    def call(self, prompt: str, model: str, resume: str | None = None, cwd: str | None = None) -> dict[str, Any]:
        if not self.binary:
            raise CursorError(self.credentials_problem() or "no binary")
        args = [self.binary, "-p", "--output-format", "json", "--sandbox", "enabled", "--trust"]
        if model and model != "auto":
            args += ["--model", model]
        if resume:
            args += ["--resume", resume]
        args.append(prompt)
        workdir = cwd or scratch_workspace()
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=self.timeout, cwd=workdir, env=env)
        except subprocess.TimeoutExpired as e:
            raise CursorError(f"cursor-agent timed out after {self.timeout}s") from e
        try:
            d = json.loads(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "")
        except (json.JSONDecodeError, IndexError) as e:
            raise CursorError(f"cursor-agent error (exit {r.returncode}): {(r.stderr or r.stdout)[:400]}") from e
        if d.get("is_error") or d.get("subtype") not in (None, "success"):
            raise CursorError(f"cursor-agent error: {str(d.get('result') or d)[:400]}")
        return d

    def conversation(self, model: str, system: str, tools: list[ToolSpec],
                     effort: str = "medium", max_tokens: int = 16000) -> Conversation:
        return CursorConversation(self, model, system, tools)

    def structured(self, model: str, system: str, prompt: str, schema: dict[str, Any],
                   effort: str = "low") -> tuple[dict[str, Any], ModelTurn]:
        text = (f"{PREAMBLE.split('Every reply')[0]}\n# Your instructions\n{system}\n\n# Input\n{prompt}\n\n"
                f"Reply with ONLY one JSON object matching this JSON schema, no prose, no code fence:\n"
                f"{json.dumps(schema)}")
        last = ""
        for _ in range(self.retries + 1):
            d = self.call(text, model)
            out = extract_json(d.get("result", ""))
            if isinstance(out, dict):
                u = _usage(d)
                return out, ModelTurn(text=json.dumps(out), tool_calls=[], stop_reason="end_turn", usage=u,
                                      cost_usd=self.cost(u), model=f"cursor:{model}")
            last = str(d.get("result", ""))[:300]
        raise CursorError(f"cursor-agent gave no JSON for a structured request: {last}")
