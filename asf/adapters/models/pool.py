"""A pool of model backends with per-role routing and failover on usage limits.

    models:
      provider: pool
      backends:
        cursor:
          provider: cursor_cli
          roles: {implementer: {model: gpt-5.3-codex-high}, triage: {model: gpt-5.3-codex-low}}
          default: {model: gpt-5.3-codex}
        claude:
          provider: claude_code
          roles: {reviewer: {model: claude-opus-5-5, effort: high}}
          default: {model: claude-opus-5-5, effort: medium}
      routing:                       # preference order per role; first usable backend wins
        default: [cursor, claude]
        reviewer: [claude, cursor]   # cross-model review: a different model family reviews

A backend is skipped while its usage limit is in force (asf.limits, shared by all projects).
When a backend hits its limit mid-job, the error names the backend; the scheduler records the
limit and retries the job, which then routes to the next backend.
"""

from __future__ import annotations

from typing import Any

from ... import limits
from ..base import AdapterContext, Conversation, ModelProvider, ModelTurn, ToolSpec

KINDS = {"claude_code": "asf.adapters.models.claude_code:ClaudeCodeProvider",
         "claude_code_native": "asf.adapters.models.claude_native:ClaudeNativeProvider",
         "cursor_cli": "asf.adapters.models.cursor_cli:CursorProvider",
         "cursor_native": "asf.adapters.models.cursor_native:CursorNativeProvider",
         "anthropic": "asf.adapters.models.anthropic_provider:AnthropicProvider",
         "scripted": "asf.adapters.models.scripted:ScriptedProvider"}


class AllBackendsLimited(RuntimeError):
    pass


def _load(kind: str) -> type:
    import importlib
    mod, _, cls = KINDS[kind].partition(":")
    return getattr(importlib.import_module(mod), cls)


class _Tagged(Conversation):
    """Marks every turn and error with the backend that produced it."""

    def __init__(self, inner: Conversation, backend: str):
        self.inner, self.backend = inner, backend

    def add_user(self, text: str) -> None:
        self.inner.add_user(text)

    def add_tool_results(self, results: list[tuple[str, str, bool]]) -> None:
        self.inner.add_tool_results(results)

    def step(self) -> ModelTurn:
        try:
            turn = self.inner.step()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"[backend:{self.backend}] {e}") from e
        if ":" not in turn.model:
            turn.model = f"{self.backend}:{turn.model}"
        return turn


class PoolProvider(ModelProvider):
    kind = "pool"

    def __init__(self, settings: dict[str, Any], ctx: AdapterContext | None = None):
        super().__init__(settings, ctx)
        self.backends: dict[str, ModelProvider] = {}
        self.specs: dict[str, dict[str, Any]] = {}
        for name, spec in (self.settings.get("backends") or {}).items():
            self.specs[name] = spec
            self.backends[name] = _load(spec["provider"])(spec, ctx)
        self.routing = self.settings.get("routing") or {"default": list(self.backends)}
        self.last_choice: dict[str, str] = {}

    def order(self, role: str) -> list[str]:
        return [b for b in (self.routing.get(role) or self.routing.get("default") or list(self.backends))
                if b in self.backends]

    def choose(self, role: str) -> tuple[str, ModelProvider, dict[str, Any]]:
        problems = []
        for name in self.order(role):
            until = limits.limited_until(name)
            if until:
                problems.append(f"{name} limited until {until.strftime('%H:%M %Z')}")
                continue
            prov = self.backends[name]
            if prov.credentials_problem():
                problems.append(f"{name}: {prov.credentials_problem()}")
                continue
            spec = self.specs[name]
            model = {**(spec.get("default") or {}), **((spec.get("roles") or {}).get(role) or {})}
            self.last_choice[role] = name
            return name, prov, model
        raise AllBackendsLimited("no model backend is usable: " + "; ".join(problems))

    def credentials_problem(self) -> str | None:
        if not self.backends:
            return "models.backends is empty"
        bad = [f"{n}: {p.credentials_problem()}" for n, p in self.backends.items() if p.credentials_problem()]
        return "; ".join(bad) if len(bad) == len(self.backends) else None

    def conversation_for(self, role: str, system: str, tools: list[ToolSpec]) -> Conversation:
        name, prov, model = self.choose(role)
        return _Tagged(prov.conversation(model.get("model", "auto"), system, tools, model.get("effort", "medium")), name)

    def native_for(self, role: str, sandbox: Any) -> tuple[str, ModelProvider, dict[str, Any]] | None:
        """The backend for `role` when it runs roles as native sessions, else None (use conversation_for)."""
        name, prov, model = self.choose(role)
        if hasattr(prov, "native_for") and prov.native_for(role, sandbox):
            return name, prov, model
        return None

    def conversation(self, model: str, system: str, tools: list[ToolSpec],
                     effort: str = "medium", max_tokens: int = 16000) -> Conversation:
        return self.conversation_for("implementer", system, tools)

    def structured_for(self, role: str, system: str, prompt: str, schema: dict[str, Any]) -> tuple[dict[str, Any], ModelTurn]:
        name, prov, model = self.choose(role)
        try:
            out, turn = prov.structured(model.get("model", "auto"), system, prompt, schema, model.get("effort", "low"))
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"[backend:{name}] {e}") from e
        if ":" not in turn.model:
            turn.model = f"{name}:{turn.model}"
        return out, turn

    def structured(self, model: str, system: str, prompt: str, schema: dict[str, Any],
                   effort: str = "low") -> tuple[dict[str, Any], ModelTurn]:
        return self.structured_for("triage", system, prompt, schema)
