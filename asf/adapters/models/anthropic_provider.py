"""Anthropic provider: manual tool loop so the broker sees every call.

Uses prompt caching on the stable system prompt and the growing conversation,
adaptive thinking with per-role effort, and server-side refusal fallbacks.
"""

from __future__ import annotations

import os

import json
from typing import Any

import anthropic

from ..base import AdapterContext, Conversation, ModelProvider, ModelTurn, ToolCall, ToolSpec

# $ per million tokens: input, output, cache read, cache write (5 minute TTL = 1.25x input)
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5),
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.5),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


class ModelRefused(RuntimeError):
    pass


def cost_of(model: str, usage: dict[str, int]) -> float:
    pin, pout, pread, pwrite = PRICES.get(model, PRICES["claude-opus-5-5"])
    return (usage.get("input", 0) * pin + usage.get("output", 0) * pout
            + usage.get("cache_read", 0) * pread + usage.get("cache_write", 0) * pwrite) / 1_000_000


def _usage(u: Any) -> dict[str, int]:
    return {"input": getattr(u, "input_tokens", 0) or 0,
            "output": getattr(u, "output_tokens", 0) or 0,
            "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0}


class AnthropicConversation(Conversation):
    def __init__(self, provider: "AnthropicProvider", model: str, system: str,
                 tools: list[ToolSpec], effort: str, max_tokens: int):
        self.p, self.model, self.effort, self.max_tokens = provider, model, effort, max_tokens
        self.system = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        self.tools = [{"name": t.name, "description": t.description, "input_schema": t.schema} for t in tools]
        self.messages: list[dict[str, Any]] = []

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_tool_results(self, results: list[tuple[str, str, bool]]) -> None:
        # All results from one assistant turn go back in a single user message.
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": text or "(empty)", "is_error": err}
            for tid, text, err in results]})

    def step(self) -> ModelTurn:
        resp = self.p.create(model=self.model, max_tokens=self.max_tokens, system=self.system,
                             tools=self.tools, messages=self.messages, effort=self.effort)
        if resp.stop_reason == "refusal":
            raise ModelRefused(f"model declined: {getattr(resp, 'stop_details', None)}")
        # Append the full content (thinking blocks included) so history stays append-only.
        self.messages.append({"role": "assistant", "content": resp.content})
        calls = [ToolCall(b.id, b.name, b.input if isinstance(b.input, dict) else json.loads(b.input or "{}"))
                 for b in resp.content if b.type == "tool_use"]
        text = "\n".join(b.text for b in resp.content if b.type == "text")
        usage = _usage(resp.usage)
        return ModelTurn(text=text, tool_calls=calls, stop_reason=resp.stop_reason or "",
                         usage=usage, cost_usd=cost_of(self.model, usage), model=resp.model)


class AnthropicProvider(ModelProvider):
    kind = "anthropic"

    def __init__(self, settings: dict[str, Any], ctx: AdapterContext | None = None):
        super().__init__(settings, ctx)
        key_env = self.settings.get("api_key_env", "ANTHROPIC_API_KEY")
        key = ctx.secrets(key_env) if ctx else None
        self._key_env = key_env
        self._has_credentials = bool(key or os.environ.get("ANTHROPIC_API_KEY")
                                     or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
        self.client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        self.fallbacks = self.settings.get("fallbacks", "default")
        self.cache = self.settings.get("prompt_cache", True)

    def credentials_problem(self) -> str | None:
        if self._has_credentials:
            return None
        return f"{self._key_env} is not set; pass --env-file with it or export it"

    def create(self, *, model: str, effort: str, **kwargs: Any) -> Any:
        kwargs["output_config"] = {**kwargs.get("output_config", {}), "effort": effort}
        if self.cache:
            kwargs["cache_control"] = {"type": "ephemeral"}
        if self.fallbacks and model in FALLBACK_MODELS:
            return self.client.beta.messages.create(model=model, betas=[FALLBACK_BETA],
                                                    fallbacks=self.fallbacks, **kwargs)
        return self.client.messages.create(model=model, **kwargs)

    def conversation(self, model: str, system: str, tools: list[ToolSpec],
                     effort: str = "medium", max_tokens: int = 16000) -> Conversation:
        return AnthropicConversation(self, model, system, tools, effort, max_tokens)

    def structured(self, model: str, system: str, prompt: str, schema: dict[str, Any],
                   effort: str = "low") -> tuple[dict[str, Any], ModelTurn]:
        resp = self.create(model=model, effort=effort, max_tokens=8000, system=system,
                           messages=[{"role": "user", "content": prompt}],
                           output_config={"format": {"type": "json_schema", "schema": schema}})
        if resp.stop_reason == "refusal":
            raise ModelRefused("model declined the structured request")
        text = next(b.text for b in resp.content if b.type == "text")
        usage = _usage(resp.usage)
        turn = ModelTurn(text=text, tool_calls=[], stop_reason=resp.stop_reason or "", usage=usage,
                         cost_usd=cost_of(model, usage), model=resp.model)
        return json.loads(text), turn
