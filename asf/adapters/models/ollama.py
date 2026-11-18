"""Ollama provider: model turns from a local model served by Ollama (`ollama serve`).

Tools are emulated exactly as in the claude_code provider: the factory's own short prompt and
JSON tool set, and every reply constrained to the turn schema with Ollama's `format` (structured
outputs), which is what makes small local models usable as agents at all. Ollama keeps no
session, so the conversation holds the message history; Ollama reuses the KV cache for the
unchanged prefix, so a turn mostly pays for the new messages.

Settings (factory.yaml, under `models:`):
    provider: ollama
    base_url: http://localhost:11434   # or env OLLAMA_HOST
    num_ctx: 32768                     # context window; Ollama's default is far too small for an agent
    timeout_s: 1800                    # per turn
    retries: 2
    think: auto                        # auto: think except at low effort; true / false to force
    keep_alive: 30m

Local models cost nothing per token; runs report $0 and are compared on resolved rate and minutes.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from ..base import AdapterContext, Conversation, ModelProvider, ModelTurn, ToolCall, ToolSpec
from .claude_code import PROTOCOL, TURN_SCHEMA


class OllamaError(RuntimeError):
    pass


def _usage(d: dict[str, Any]) -> dict[str, int]:
    return {"input": int(d.get("prompt_eval_count") or 0), "output": int(d.get("eval_count") or 0),
            "cache_read": 0, "cache_write": 0}


def _parse(content: str) -> dict[str, Any] | None:
    try:
        out = json.loads(content)
    except (TypeError, ValueError):
        return None
    return out if isinstance(out, dict) else None


class OllamaConversation(Conversation):
    def __init__(self, provider: "OllamaProvider", model: str, system: str, tools: list[ToolSpec],
                 effort: str = "medium"):
        self.p, self.model, self.think = provider, model, provider.thinks(effort)
        specs = "\n".join(f"- {t.name}: {t.description}\n  schema: {json.dumps(t.schema)}" for t in tools)
        self.messages: list[dict[str, str]] = [{"role": "system", "content": system + PROTOCOL + specs}]
        self.pending: list[str] = []
        self.n = 0

    def add_user(self, text: str) -> None:
        self.pending.append(text)

    def add_tool_results(self, results: list[tuple[str, str, bool]]) -> None:
        for tid, text, err in results:
            self.pending.append(f'<tool_result id="{tid}" error="{str(err).lower()}">\n{text or "(empty)"}\n</tool_result>')

    def step(self) -> ModelTurn:
        self.messages.append({"role": "user", "content": "\n\n".join(self.pending) or "Continue."})
        self.pending = []
        usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        last = ""
        for _ in range(self.p.retries + 1):
            d = self.p.chat(self.model, self.messages, TURN_SCHEMA, self.think)
            for k, v in _usage(d).items():
                usage[k] += v
            content = ((d.get("message") or {}).get("content") or "").strip()
            out = _parse(content)
            if out is not None and isinstance(out.get("tool_calls"), list):
                self.messages.append({"role": "assistant", "content": content})
                calls = []
                for c in out["tool_calls"]:
                    if not isinstance(c, dict):
                        continue
                    self.n += 1
                    inp = c.get("input") if isinstance(c.get("input"), dict) else {}
                    calls.append(ToolCall(f"call_{self.n}", str(c.get("name", "")), inp))
                return ModelTurn(text=str(out.get("text", "")), tool_calls=calls,
                                 stop_reason="tool_use" if calls else "end_turn",
                                 usage=usage, cost_usd=0.0, model=self.model)
            last = content[:300] or str(d.get("done_reason") or "empty reply")
            self.messages.append({"role": "assistant", "content": content or "(no reply)"})
            self.messages.append({"role": "user", "content": "Your last reply was not a JSON object with "
                                  "\"text\" and \"tool_calls\". Reply again in that format."})
        raise OllamaError(f"no valid reply after {self.p.retries + 1} tries: {last}")


class OllamaProvider(ModelProvider):
    kind = "ollama"

    def __init__(self, settings: dict[str, Any], ctx: AdapterContext | None = None):
        super().__init__(settings, ctx)
        host = self.settings.get("base_url") or os.environ.get("OLLAMA_HOST") or "http://localhost:11434"
        self.base_url = (host if "://" in host else "http://" + host).rstrip("/")
        self.num_ctx = int(self.settings.get("num_ctx", 32768))
        self.timeout = int(self.settings.get("timeout_s", 1800))
        self.retries = int(self.settings.get("retries", 2))
        self.think = self.settings.get("think", "auto")
        self.keep_alive = str(self.settings.get("keep_alive", "30m"))

    def credentials_problem(self) -> str | None:
        try:
            with urllib.request.urlopen(self.base_url + "/api/tags", timeout=5) as r:
                json.loads(r.read())
        except (OSError, ValueError) as e:
            return f"Ollama is not answering at {self.base_url} ({e}): start it with `ollama serve`"
        return None

    def thinks(self, effort: str) -> bool:
        if isinstance(self.think, bool):
            return self.think
        return effort not in ("low", "minimal", "")

    def chat(self, model: str, messages: list[dict[str, str]], schema: dict[str, Any],
             think: bool = False) -> dict[str, Any]:
        body = {"model": model, "messages": messages, "stream": False, "format": schema,
                "think": think, "keep_alive": self.keep_alive,
                "options": {"num_ctx": self.num_ctx, "temperature": 0.2}}
        req = urllib.request.Request(self.base_url + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise OllamaError(f"ollama {e.code}: {e.read()[:300]!r}") from e
        except (OSError, ValueError) as e:
            raise OllamaError(f"ollama request failed: {e}") from e
        if d.get("error"):
            raise OllamaError(f"ollama: {str(d['error'])[:300]}")
        return d

    def conversation(self, model: str, system: str, tools: list[ToolSpec],
                     effort: str = "medium", max_tokens: int = 16000) -> Conversation:
        return OllamaConversation(self, model, system, tools, effort)

    def structured(self, model: str, system: str, prompt: str, schema: dict[str, Any],
                   effort: str = "low") -> tuple[dict[str, Any], ModelTurn]:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        last = ""
        for _ in range(self.retries + 1):
            d = self.chat(model, msgs, schema, self.thinks(effort))
            content = ((d.get("message") or {}).get("content") or "").strip()
            out = _parse(content)
            if out is not None:
                return out, ModelTurn(text=content, tool_calls=[], stop_reason="end_turn",
                                      usage=_usage(d), cost_usd=0.0, model=model)
            last = content[:300]
        raise OllamaError(f"no structured output: {last}")
