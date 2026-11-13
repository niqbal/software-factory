"""Variants: what is being compared.

A variant is a named overlay on the case's `factory.yaml`, so an A/B test is a config change and
nothing else. A file in the archive's `variants/` folder overrides a built-in of the same name:

    name: opus-high
    description: every role on Opus at high effort
    replace: [models]          # top-level sections replaced wholesale instead of deep-merged
    overlay:
      models: {provider: claude_code, roles: {...}}

`kind: interactive` is the baseline: no factory, plain `claude -p` with its default tools in the
checkout, on the ticket text alone. Its settings: model, effort, permission_mode, allowed_tools,
timeout_s, binary, extra_args.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..adapters import base as adapters
from ..config import deep_merge
from ..util import sha
from .cases import EvalError

CLAUDE_ROLES = {
    "triage": {"model": "claude-haiku-5-5", "effort": "low"},
    "implementer": {"model": "claude-opus-5-5", "effort": "high"},
    "tester": {"model": "claude-opus-5-5", "effort": "medium"},
    "reviewer": {"model": "claude-opus-5-5", "effort": "high"},
    "security": {"model": "claude-sonnet-5-5", "effort": "medium"},
    "ci_fixer": {"model": "claude-opus-5-5", "effort": "medium"},
}
# Cursor variants run Grok 4.7, effort per role as the Claude variants set it.
CURSOR_ROLES = {
    "triage": {"model": "grok-4.7-low"},
    "implementer": {"model": "grok-4.7-high"},
    "tester": {"model": "grok-4.7-medium"},
    "reviewer": {"model": "grok-4.7-high"},
    "security": {"model": "grok-4.7-medium"},
    "ci_fixer": {"model": "grok-4.7-medium"},
}
OPUS_HIGH = {r: {"model": "claude-opus-5-5", "effort": "high"} for r in CLAUDE_ROLES}

BUILTIN: dict[str, dict[str, Any]] = {
    "current": {"description": "the case's own factory.yaml, unchanged", "overlay": {}},
    "claude_code": {"description": "every role through claude -p with tools emulated as JSON (today's harness)",
                    "replace": ["models"],
                    "overlay": {"models": {"provider": "claude_code", "timeout_s": 900, "roles": CLAUDE_ROLES}}},
    "claude_code_native": {"description": "implementer, tester and CI fixer as real Claude Code sessions (Phase 1)",
                           "replace": ["models"],
                           "overlay": {"models": {"provider": "claude_code_native", "timeout_s": 900,
                                                  "roles": CLAUDE_ROLES}}},
    "cursor_cli": {"description": "every role through cursor-agent -p (Grok 4.7) with tools emulated as JSON",
                   "replace": ["models"],
                   "overlay": {"models": {"provider": "cursor_cli", "timeout_s": 900, "roles": CURSOR_ROLES}}},
    "cursor_native": {"description": "implementer, tester and CI fixer as real cursor-agent sessions on Grok 4.7 (Phase 1)",
                      "replace": ["models"],
                      "overlay": {"models": {"provider": "cursor_native", "timeout_s": 900, "roles": CURSOR_ROLES}}},
    "opus-high": {"description": "claude_code with every role on Opus at high effort",
                  "replace": ["models"],
                  "overlay": {"models": {"provider": "claude_code", "timeout_s": 900, "roles": OPUS_HIGH}}},
    "interactive": {"description": "baseline: plain claude -p with default tools on the ticket text, no factory",
                    "kind": "interactive", "settings": {}},
}

# Tools an interactive user lets Claude Code use. Bash is allowed outright: in `-p` mode nothing
# can prompt, and a person at the keyboard approves test runs and the like.
INTERACTIVE_TOOLS = ["Bash", "Read", "Edit", "MultiEdit", "Write", "Glob", "Grep", "LS", "TodoWrite",
                     "Task", "NotebookEdit"]
# Backend names for usage limits (asf/limits.py), as the scheduler names them.
BACKEND_OF = {"claude_code": "claude", "claude_code_native": "claude", "cursor_cli": "cursor",
              "cursor_native": "cursor"}


@dataclass
class Variant:
    name: str
    kind: str = "factory"                  # factory | interactive
    description: str = ""
    overlay: dict[str, Any] = field(default_factory=dict)
    replace: list[str] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)   # interactive only
    source: str = "built-in"

    @property
    def hash(self) -> str:
        return sha(yaml.safe_dump({"kind": self.kind, "overlay": self.overlay, "replace": self.replace,
                                   "settings": self.settings}, sort_keys=True), 10)

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        """The case's factory.yaml with this variant laid over it."""
        out = copy.deepcopy(raw)
        for key in self.replace:
            if key in self.overlay:
                out[key] = copy.deepcopy(self.overlay[key])
        rest = {k: v for k, v in self.overlay.items() if k not in self.replace}
        return deep_merge(out, rest)

    def providers(self, raw: dict[str, Any]) -> list[str]:
        """Model provider kinds this variant needs once applied to `raw`."""
        if self.kind == "interactive":
            return []
        models = self.apply(raw).get("models") or {}
        kind = models.get("provider", "anthropic")
        if kind == "pool":
            return [str((b or {}).get("provider", "")) for b in (models.get("backends") or {}).values()]
        return [kind]

    def unavailable(self, raw: dict[str, Any]) -> str | None:
        """Why this variant cannot run in this build of asf, or None.

        The native providers ship on their own branch; until it merges, their variants say so
        instead of failing halfway through a case."""
        from ..adapters.models import pool
        in_pool = (self.apply(raw).get("models") or {}).get("provider") == "pool"
        for kind in self.providers(raw):
            try:
                adapters.load("models", kind)
            except KeyError:
                return f"variant {self.name} needs models.provider {kind}, which this build of asf does not have"
            if in_pool and kind not in getattr(pool, "KINDS", {}):
                return f"variant {self.name}: the pool cannot run a {kind} backend in this build of asf"
        return None

    def backends(self, raw: dict[str, Any]) -> list[str]:
        """Usage-limit backends (claude, cursor, ...) this variant draws on."""
        if self.kind == "interactive":
            return ["claude"]
        models = self.apply(raw).get("models") or {}
        if models.get("provider") == "pool":
            return list((models.get("backends") or {}).keys())
        kind = models.get("provider", "anthropic")
        return [BACKEND_OF.get(kind, kind)]


def parse_variant(name: str, data: dict[str, Any], source: str) -> Variant:
    if not isinstance(data, dict):
        raise EvalError(f"variant {name} ({source}) must be a mapping")
    kind = data.get("kind", "factory")
    if kind not in ("factory", "interactive"):
        raise EvalError(f"variant {name}: kind must be factory or interactive")
    overlay = data.get("overlay") or {}
    if not isinstance(overlay, dict):
        raise EvalError(f"variant {name}: overlay must be a mapping of factory.yaml sections")
    return Variant(name=str(data.get("name") or name), kind=kind, description=str(data.get("description") or ""),
                   overlay=overlay, replace=list(data.get("replace") or []),
                   settings=dict(data.get("settings") or {}), source=source)


def available(root: Path) -> dict[str, Variant]:
    out = {name: parse_variant(name, copy.deepcopy(d), "built-in") for name, d in BUILTIN.items()}
    folder = root / "variants"
    if folder.is_dir():
        for p in sorted(folder.glob("*.y*ml")):
            v = parse_variant(p.stem, yaml.safe_load(p.read_text()) or {}, str(p))
            out[v.name] = v
    return out


def load(root: Path, name: str) -> Variant:
    vs = available(root)
    if name not in vs:
        raise EvalError(f"unknown variant {name!r}. Built in: {', '.join(BUILTIN)}; "
                        f"or add {root / 'variants' / (name + '.yaml')}")
    return vs[name]
