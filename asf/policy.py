"""Policy engine: risk tiers, required gates, approvals and merge rights.

Everything here is a pure function of factory.yaml plus facts about the change,
so it is unit-testable and identical wherever the factory runs.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .models import WorkItem
from .util import any_match

RISK_TO_TIER = {"low": 1, "medium": 2, "high": 3}


def tier_for_path(cfg: Config, path: str) -> int | None:
    """Highest tier among rules matching the path, or None if no rule matches."""
    tiers = [r["tier"] for r in cfg.get("tiers.rules", []) if any_match(path, r["paths"])]
    return max(tiers) if tiers else None


def tier_for_paths(cfg: Config, paths: list[str]) -> int:
    if not paths:
        return 0
    default = cfg.get("tiers.classifier_default", 2)
    return max((t if (t := tier_for_path(cfg, p)) is not None else default) for p in paths)


def label_override(cfg: Config, item: WorkItem) -> int | None:
    prefix = cfg.get("tiers.override_label_prefix", "tier:")
    for label in item.labels:
        if label.startswith(prefix):
            try:
                value = int(label[len(prefix):])
            except ValueError:
                continue
            if 0 <= value <= 3:
                return value
    return None


def pre_tier(cfg: Config, item: WorkItem, triage_guess: int | None = None) -> int:
    """Tier before any code exists: label override, then risk hint, then triage."""
    override = label_override(cfg, item)
    if override is not None:
        return override
    candidates = [t for t in (RISK_TO_TIER.get(item.risk_hint or ""), triage_guess) if t is not None]
    if item.kind == "incident":
        candidates.append(2)
    return max(candidates) if candidates else 1


def final_tier(cfg: Config, item: WorkItem, pre: int, changed: list[str]) -> int:
    """A human label always wins. Otherwise the diff can only raise the tier, never lower it."""
    override = label_override(cfg, item)
    if override is not None:
        return override
    return max(pre, tier_for_paths(cfg, changed))


@dataclass
class Decision:
    tier: int
    deterministic: list[str]
    llm: list[str]
    approvals_required: int
    merge_mode: str          # agent | agent_after_approval | human
    may_agent_merge: bool
    shadow: bool
    reason: str

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def decide(cfg: Config, tier: int, manual_checks: list[str] | None = None) -> Decision:
    g = cfg.gates_for(tier)
    shadow = bool(cfg.get("autonomy.shadow", True))
    max_merge = cfg.get("autonomy.max_merge_tier", -1)
    merge_mode = g.get("merge", "human")
    may_merge = (not shadow) and tier <= max_merge and merge_mode != "human" and not manual_checks
    reasons = []
    if manual_checks:
        reasons.append("manual verification required before merge")
    if shadow:
        reasons.append("shadow mode: nothing merges")
    elif tier > max_merge:
        reasons.append(f"tier {tier} above autonomy.max_merge_tier {max_merge}")
    if merge_mode == "human":
        reasons.append(f"tier {tier} merges are human-only")
    return Decision(
        tier=tier,
        deterministic=list(g.get("deterministic", [])),
        llm=list(g.get("llm", [])),
        approvals_required=int(g.get("human_approvals", 0)),
        merge_mode=merge_mode,
        may_agent_merge=may_merge,
        shadow=shadow,
        reason="; ".join(reasons) or "agent may merge after gates",
    )


def may_edit_existing_tests(cfg: Config, tier: int) -> bool:
    return tier <= cfg.get("permissions.implementer.may_edit_existing_tests_up_to_tier", 0)


def new_dependencies_allowed(cfg: Config, tier: int) -> bool:
    return tier >= cfg.get("limits.allow_new_dependencies_from_tier", 2)
