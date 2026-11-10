"""Adapter interfaces and registry.

The core imports only this module. Each external system is reached through one
of these interfaces; vendor code lives in the subpackages and is loaded by name
from factory.yaml. Third parties can register adapters through the
``asf.adapters`` entry-point group as ``<category>.<kind> = module:Class``.
"""

from __future__ import annotations

import importlib
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..models import GateResult, WorkItem
from ..util import ExecResult

if TYPE_CHECKING:
    from ..config import Config
    from ..store import Store


@dataclass
class AdapterContext:
    repo: Path
    state_dir: Path
    config: "Config"
    store: "Store | None" = None
    secrets: Callable[[str], str | None] = field(default=lambda name: os.environ.get(name))
    runner: Callable[..., ExecResult] | None = None   # injectable for tests


# ---------------------------------------------------------------------------
# Intake: external signal -> WorkItem
# ---------------------------------------------------------------------------

class IntakeAdapter(ABC):
    kind: str = ""

    def __init__(self, settings: dict[str, Any], ctx: AdapterContext):
        self.settings = settings or {}
        self.ctx = ctx

    def poll(self) -> list[WorkItem]:
        """Pull mode: return items that are ready for the factory."""
        return []

    def fetch(self, external_id: str) -> WorkItem:
        raise NotImplementedError(f"{self.kind} cannot fetch single items")

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        """Push mode: check the sender's signature. Adapters without push return False."""
        return False

    def normalize_webhook(self, headers: dict[str, str], body: bytes) -> list[WorkItem]:
        return []

    def report(self, item: WorkItem, status: str, message: str) -> None:
        """Optional write-back (comment, status). Off unless settings.write_back is true."""


# ---------------------------------------------------------------------------
# Execution: an isolated checkout where agents and gates run
# ---------------------------------------------------------------------------

class Sandbox(ABC):
    root: Path
    branch: str
    base_sha: str

    @abstractmethod
    def exec(self, command: str, cwd: str = ".", timeout: float = 900) -> ExecResult: ...

    @abstractmethod
    def changed_files(self) -> list[str]: ...

    @abstractmethod
    def diff(self, paths: list[str] | None = None) -> str: ...

    @abstractmethod
    def numstat(self) -> list[tuple[int, int, str]]: ...

    @abstractmethod
    def file_at_base(self, path: str) -> str | None: ...

    @abstractmethod
    def commit(self, message: str) -> str: ...

    @abstractmethod
    def destroy(self) -> None: ...

    def reset(self, to: str | None = None) -> None:
        """Discard all changes and return to `to` (default: the base commit)."""
        raise NotImplementedError

    def uncommitted_files(self) -> list[str]:
        """Files changed since the branch tip (work not yet committed)."""
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {"root": str(self.root), "branch": self.branch, "base_sha": self.base_sha}


class SandboxProvider(ABC):
    kind: str = ""

    def __init__(self, settings: dict[str, Any], ctx: AdapterContext):
        self.settings = settings or {}
        self.ctx = ctx

    @abstractmethod
    def create(self, job_id: str, base_ref: str, branch: str) -> Sandbox: ...

    @abstractmethod
    def reopen(self, info: dict[str, Any]) -> Sandbox: ...

    def base_checkout(self, base_sha: str) -> Sandbox:
        """A throwaway sandbox at the base commit, used to tell new failures from old ones."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Delivery: branch/PR/merge in the company's SCM
# ---------------------------------------------------------------------------

class DeliveryAdapter(ABC):
    kind: str = ""

    def __init__(self, settings: dict[str, Any], ctx: AdapterContext):
        self.settings = settings or {}
        self.ctx = ctx

    @abstractmethod
    def deliver(self, job: dict[str, Any], item: WorkItem, sandbox: Sandbox,
                report_md: str, draft: bool) -> dict[str, Any]: ...

    def merge(self, job: dict[str, Any], delivery: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(f"{self.kind} cannot merge")


# ---------------------------------------------------------------------------
# Observe: post-deploy signal (Sentry, Datadog...)
# ---------------------------------------------------------------------------

class ObserveAdapter(ABC):
    kind: str = ""

    def __init__(self, settings: dict[str, Any], ctx: AdapterContext):
        self.settings = settings or {}
        self.ctx = ctx

    @abstractmethod
    def check(self, job: dict[str, Any], since_ts: float) -> GateResult: ...


# ---------------------------------------------------------------------------
# Notify: Slack, console, ...
# ---------------------------------------------------------------------------

class Notifier(ABC):
    kind: str = ""

    def __init__(self, settings: dict[str, Any], ctx: AdapterContext):
        self.settings = settings or {}
        self.ctx = ctx

    @abstractmethod
    def notify(self, event: str, job: dict[str, Any], message: str) -> None: ...


# ---------------------------------------------------------------------------
# Models: one conversation interface over any provider
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ModelTurn:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    usage: dict[str, int]
    cost_usd: float
    model: str


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]


class Conversation(ABC):
    """Provider-owned message history. The agent loop only appends to it."""

    @abstractmethod
    def add_user(self, text: str) -> None: ...

    @abstractmethod
    def add_tool_results(self, results: list[tuple[str, str, bool]]) -> None: ...

    @abstractmethod
    def step(self) -> ModelTurn: ...


class ModelProvider(ABC):
    kind: str = ""

    def __init__(self, settings: dict[str, Any], ctx: AdapterContext | None = None):
        self.settings = settings or {}
        self.ctx = ctx

    def credentials_problem(self) -> str | None:
        """Why model calls would fail for lack of credentials, or None. Checked before work starts,
        so a missing key stops a run cleanly instead of failing a job halfway."""
        return None

    @abstractmethod
    def conversation(self, model: str, system: str, tools: list[ToolSpec],
                     effort: str = "medium", max_tokens: int = 16000) -> Conversation: ...

    @abstractmethod
    def structured(self, model: str, system: str, prompt: str, schema: dict[str, Any],
                   effort: str = "low") -> tuple[dict[str, Any], ModelTurn]: ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

BUILTIN: dict[str, dict[str, str]] = {
    "intake": {
        "local": "asf.adapters.intake.local_file:LocalFileIntake",
        "beads": "asf.adapters.intake.beads:BeadsIntake",
        "github_issues": "asf.adapters.intake.github_issues:GitHubIssuesIntake",
        "linear": "asf.adapters.intake.linear:LinearIntake",
        "jira": "asf.adapters.intake.jira:JiraIntake",
        "slack": "asf.adapters.intake.slack:SlackIntake",
        "sentry": "asf.adapters.intake.sentry:SentryIntake",
        "pagerduty": "asf.adapters.intake.pagerduty:PagerDutyIntake",
    },
    "sandbox": {
        "local_worktree": "asf.adapters.sandbox.local_worktree:LocalWorktreeProvider",
        "docker": "asf.adapters.sandbox.docker:DockerProvider",
    },
    "delivery": {
        "local_git": "asf.adapters.delivery.local_git:LocalGitDelivery",
        "github": "asf.adapters.delivery.github:GitHubDelivery",
        "gitlab": "asf.adapters.delivery.gitlab:GitLabDelivery",
    },
    "notify": {
        "console": "asf.adapters.notify.console:ConsoleNotifier",
        "slack_webhook": "asf.adapters.notify.slack_webhook:SlackWebhookNotifier",
    },
    "observe": {
        "none": "asf.adapters.observe:NoObserve",
        "sentry": "asf.adapters.observe:SentryObserve",
    },
    "models": {
        "anthropic": "asf.adapters.models.anthropic_provider:AnthropicProvider",
        "scripted": "asf.adapters.models.scripted:ScriptedProvider",
    },
}


def available(category: str) -> list[str]:
    names = set(BUILTIN.get(category, {}))
    for ep in entry_points(group="asf.adapters"):
        cat, _, kind = ep.name.partition(".")
        if cat == category:
            names.add(kind)
    return sorted(names)


def load(category: str, kind: str) -> type:
    target = BUILTIN.get(category, {}).get(kind)
    if target is None:
        for ep in entry_points(group="asf.adapters"):
            if ep.name == f"{category}.{kind}":
                return ep.load()
        raise KeyError(f"no {category} adapter named '{kind}'. Available: {available(category)}")
    module, _, cls = target.partition(":")
    return getattr(importlib.import_module(module), cls)
