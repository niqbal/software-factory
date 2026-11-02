"""Normalized domain objects. Every adapter converts to and from these."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class WorkItem:
    """A unit of requested work, normalized from any intake source.

    ``body`` is untrusted text (tickets, alerts, chat). It is shown to agents
    as quoted data and never treated as instructions to the factory.
    """

    source_kind: str            # github_issues | beads | linear | jira | slack | sentry | pagerduty | local
    external_id: str            # id in the source system
    title: str
    body: str = ""
    url: str = ""
    repo: str = ""              # owner/name or local path, resolved by routing
    acceptance: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    reporter: str = ""
    kind: str = "task"          # bug | feature | task | chore | incident
    priority: int | None = None
    risk_hint: str | None = None   # low | medium | high
    budget_hint: dict[str, Any] | None = None
    id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WorkItem":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


# Job state machine. Order matters: resume re-enters at the stored state.
STATES = [
    "queued",             # waiting for triage
    "triaged",            # tier guessed, acceptance criteria fixed, sandbox not yet created
    "implementing",
    "testing",            # independent tester
    "verifying",          # deterministic gates, CI fixer on failure
    "reviewing",          # LLM reviewers required by the tier
    "committing",
    "delivering",         # branch / draft PR
    "awaiting_approval",  # only when the agent is allowed to merge after approval
    "merging",
    "observing",          # post-deploy gate
    "done",
]
TERMINAL = {"done", "failed", "rejected", "needs_human"}
HALTED = {"paused"}


@dataclass
class GateResult:
    name: str
    status: str                 # pass | fail | skipped | preexisting
    detail: str = ""
    log: str = ""
    package: str = ""

    @property
    def blocking(self) -> bool:
        return self.status == "fail"


@dataclass
class ReviewFinding:
    severity: str               # blocking | major | minor | nit
    file: str
    issue: str
    line: int | None = None


@dataclass
class AgentResult:
    role: str
    ok: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    stop: str = ""
