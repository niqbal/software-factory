"""Role prompts and the agent loop."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from string import Template
from typing import Any

from .. import policy
from ..adapters.base import ModelProvider
from ..broker import ToolBroker
from ..config import Config
from ..models import AgentResult, WorkItem
from . import context

TITLES = {"implementer": "implementer", "tester": "independent tester", "reviewer": "code reviewer",
          "security": "security reviewer", "ci_fixer": "CI fixer", "triage": "triage agent"}


def _template(name: str) -> str:
    return (files("asf.agents") / "prompts" / f"{name}.md").read_text()


class PromptBuilder:
    """Builds role prompts. The shared part is computed once per job so it caches."""

    def __init__(self, cfg: Config, root: Path):
        self.cfg = cfg
        self.vars = {
            "project": cfg.get("project.name") or root.name,
            "conventions": context.conventions(root, cfg),
            "layout": context.layout(root),
            "commands": context.commands(cfg),
        }

    def system(self, role: str, tier: int) -> str:
        test_rule = ("you may edit them at this tier." if policy.may_edit_existing_tests(self.cfg, tier)
                     else "do not edit them. Add new test files or new test cases in new files.")
        values = {**self.vars, "role": role, "role_title": TITLES[role], "test_edit_rule": test_rule,
                  "max_files": self.cfg.get("permissions.ci_fixer.max_files_touched", 5)}
        if role == "triage":
            return Template(_template("triage")).safe_substitute(values)
        return Template(_template("_common") + _template(role)).safe_substitute(values)


def work_item_block(item: WorkItem, acceptance: list[str]) -> str:
    crit = "\n".join(f"- {c}" for c in acceptance) or "- (none stated)"
    meta = f"source: {item.source_kind} {item.external_id}" + (f" ({item.url})" if item.url else "")
    return (f"<work_item>\n{meta}\nkind: {item.kind}\ntitle: {item.title}\n\n{item.body.strip()}\n"
            f"</work_item>\n\nAcceptance criteria:\n{crit}")


class BudgetExceeded(RuntimeError):
    pass


def run_agent(provider: ModelProvider, cfg: Config, role: str, system: str, task: str,
              broker: ToolBroker, charge: Any, max_turns: int | None = None) -> AgentResult:
    """Drive one role until it calls finish/submit_review, runs out of turns, or hits budget.

    ``charge(role, turn)`` records spend and raises BudgetExceeded when a cap is hit.
    """
    spec = cfg.role_model(role)
    max_turns = max_turns or cfg.get(f"models.max_turns.{role}", 40)
    if hasattr(provider, "conversation_for"):  # a pool routes each role to a backend
        conv = provider.conversation_for(role, system, broker.tools())
    else:
        conv = provider.conversation(spec["model"], system, broker.tools(), spec.get("effort", "medium"))
    conv.add_user(task)
    result = AgentResult(role=role, ok=False, summary="")
    nudged = False
    for n in range(1, max_turns + 1):
        turn = conv.step()
        result.turns = n
        result.input_tokens += turn.usage.get("input", 0) + turn.usage.get("cache_read", 0)
        result.output_tokens += turn.usage.get("output", 0)
        result.cost_usd += turn.cost_usd
        result.stop = turn.stop_reason
        charge(role, turn)
        if turn.tool_calls:
            outputs = [(c.id, *broker.call(c.name, c.input)) for c in turn.tool_calls]
            conv.add_tool_results(outputs)
            if broker.state.finished:
                break
            continue
        if turn.stop_reason == "max_tokens":
            conv.add_user("Your reply was cut off. Continue in smaller steps.")
            continue
        if not nudged:
            finisher = "submit_review" if role in ("reviewer", "security") else "finish"
            # Worded so the model does not read it as an order to stop: an earlier "call finish
            # now" made an implementer give up mid-task and report itself blocked.
            conv.add_user(f"You ended your turn without calling {finisher}. If the task is not done, "
                          f"keep working with the tools. Call {finisher} only when it is done, or "
                          "when a human must decide something you cannot.")
            nudged = True
            continue
        break
    result.data = dict(broker.state.result)
    result.data["files_touched"] = sorted(broker.state.touched)
    result.data["denials"] = broker.state.denials
    result.data["finished"] = broker.state.finished
    result.data["nudged"] = nudged
    result.summary = result.data.get("summary", "")
    result.ok = broker.state.finished and not result.data.get("blocked_reason")
    if not broker.state.finished:
        result.summary = result.summary or f"{role} stopped after {result.turns} turns without finishing"
    return result
