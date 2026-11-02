"""Deterministic provider for tests and dry runs. No network, no spend.

Scripts are keyed by role. Each conversation for a role pops the next script,
and each script is a list of turns; a turn is a list of tool calls.
"""

from __future__ import annotations

import copy
from typing import Any, Callable

from ..base import Conversation, ModelProvider, ModelTurn, ToolCall, ToolSpec

TurnFn = Callable[[list[tuple[str, str, bool]]], list[dict[str, Any]]]


class ScriptedConversation(Conversation):
    def __init__(self, role: str, turns: list[Any]):
        self.role, self.turns, self.i = role, turns, 0
        self.inbox: list[str] = []
        self.results: list[list[tuple[str, str, bool]]] = []

    def add_user(self, text: str) -> None:
        self.inbox.append(text)

    def add_tool_results(self, results: list[tuple[str, str, bool]]) -> None:
        self.results.append(results)

    def step(self) -> ModelTurn:
        if self.i >= len(self.turns):
            return ModelTurn("done", [], "end_turn", {"input": 1, "output": 1}, 0.0, "scripted")
        turn = self.turns[self.i]
        self.i += 1
        if callable(turn):
            turn = turn(self.results[-1] if self.results else [])
        calls = [ToolCall(f"call_{self.i}_{n}", c["name"], c.get("input", {})) for n, c in enumerate(turn)]
        return ModelTurn("", calls, "tool_use" if calls else "end_turn",
                         {"input": 100, "output": 50}, 0.001, "scripted")


class ScriptedProvider(ModelProvider):
    kind = "scripted"

    def __init__(self, settings: dict[str, Any] | None = None, ctx=None):
        super().__init__(settings or {}, ctx)
        self.scripts: dict[str, list[list[Any]]] = copy.deepcopy(self.settings.get("scripts", {}))
        self.structured_answers: dict[str, dict[str, Any]] = self.settings.get("structured", {})
        self.log: list[tuple[str, str]] = []

    def conversation(self, model: str, system: str, tools: list[ToolSpec],
                     effort: str = "medium", max_tokens: int = 16000) -> Conversation:
        role = system.split("[role:", 1)[1].split("]", 1)[0] if "[role:" in system else "unknown"
        self.log.append((role, model))
        queue = self.scripts.get(role, [])
        return ScriptedConversation(role, queue.pop(0) if queue else [])

    def structured(self, model: str, system: str, prompt: str, schema: dict[str, Any],
                   effort: str = "low") -> tuple[dict[str, Any], ModelTurn]:
        role = system.split("[role:", 1)[1].split("]", 1)[0] if "[role:" in system else "triage"
        self.log.append((role, model))
        answer = self.structured_answers.get(role, {})
        return copy.deepcopy(answer), ModelTurn("", [], "end_turn", {"input": 10, "output": 10}, 0.0001, "scripted")
