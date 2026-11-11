"""Load, default and validate factory.yaml.

The file is the single place a company encodes its trust boundary. Anything
not set falls back to safe defaults: shadow mode on, nothing merges, no new
dependencies at low tiers, agents cannot edit existing tests or CI config.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .util import any_match, sha

DEFAULT_TEST_GLOBS = [
    "**/*.test.*", "**/*.spec.*", "**/__tests__/**", "**/test_*.py", "**/*_test.py",
    "**/*_test.go", "tests/**", "test/**", "**/src/test/**",
]

DEFAULTS: dict[str, Any] = {
    "version": 1,
    "project": {"name": "", "default_branch": "main"},
    # Each package is a unit with its own commands. Single-package repos have one
    # entry with root "." ; monorepos list several and gates run only where the diff lands.
    "packages": [],
    "tests": {"globs": DEFAULT_TEST_GLOBS},
    "intake": {},
    "execution": {
        "provider": "local_worktree",
        "clone_dirs": [],          # e.g. node_modules: copy-on-write cloned into the sandbox
        "setup": [],               # commands run once per sandbox before agents start
        "command_timeout_s": 900,
        "docker": {"image": "node:22-bookworm", "network": "none"},
    },
    "models": {
        "provider": "anthropic",
        "api_key_env": "ANTHROPIC_API_KEY",
        "roles": {
            "triage": {"model": "claude-haiku-5-5", "effort": "low"},
            "implementer": {"model": "claude-opus-5-5", "effort": "high"},
            "tester": {"model": "claude-opus-5-5", "effort": "medium"},
            "reviewer": {"model": "claude-opus-5-5", "effort": "high"},
            "security": {"model": "claude-opus-5-5", "effort": "medium"},
            "ci_fixer": {"model": "claude-opus-5-5", "effort": "medium"},
        },
        "fallbacks": "default",
        "prompt_cache": True,
        "max_turns": {"implementer": 60, "tester": 40, "reviewer": 25, "security": 20, "ci_fixer": 30},
    },
    "tiers": {
        "rules": [
            {"paths": ["docs/**", "**/*.md", "CHANGELOG*", "LICENSE*"], "tier": 0},
        ],
        "classifier_default": 2,
        "override_label_prefix": "tier:",
    },
    "gates": {
        "tier_0": {"deterministic": ["secret_scan", "diff_size"], "llm": [],
                   "human_approvals": 0, "merge": "agent"},
        "tier_1": {"deterministic": ["secret_scan", "diff_size", "test_integrity", "dependency_policy",
                                     "lint", "typecheck", "build", "test"],
                   "llm": ["reviewer"], "human_approvals": 0, "merge": "agent"},
        "tier_2": {"deterministic": ["secret_scan", "diff_size", "test_integrity", "dependency_policy",
                                     "lint", "typecheck", "build", "test"],
                   "llm": ["reviewer", "security"], "human_approvals": 1, "merge": "agent_after_approval"},
        "tier_3": {"deterministic": ["secret_scan", "diff_size", "test_integrity", "dependency_policy",
                                     "lint", "typecheck", "build", "test"],
                   "llm": ["reviewer", "security"], "human_approvals": 1, "merge": "human"},
    },
    "limits": {
        "max_changed_lines": {"tier_0": 400, "tier_1": 600, "tier_2": 1200, "tier_3": 1500},
        "allow_new_dependencies_from_tier": 2,
        "require_tests_for_code_changes": True,
        "fix_rounds": 1,           # implementer re-runs after tester/reviewer findings
        "ci_fix_rounds": 1,
    },
    "autonomy": {"shadow": True, "max_merge_tier": -1},
    "budgets": {
        "tier_0": {"usd": 0.75, "minutes": 15},
        "tier_1": {"usd": 2.0, "minutes": 30},
        "tier_2": {"usd": 5.0, "minutes": 90},
        "tier_3": {"usd": 8.0, "minutes": 150},
        "monthly_cap_usd": 50.0,
    },
    "delivery": {"provider": "local_git", "branch_prefix": "factory/", "draft": True},
    "observe": {"provider": "none"},
    "permissions": {
        "protected_paths": [".github/**", ".gitlab-ci.yml", ".circleci/**", "factory.yaml",
                            "**/.env", "**/.env.*", "**/*.pem", "**/*.key"],
        "secret_files": ["**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/id_rsa*",
                         "**/google-services.json", "**/*.keystore", "**/*.jks"],
        "implementer": {"may_edit_existing_tests_up_to_tier": 0},
        "ci_fixer": {"max_files_touched": 5},
        "extra_allowed_commands": [],
        "denied_command_patterns": [],
    },
    "notifications": {"providers": ["console"], "on": ["needs_human", "awaiting_approval",
                                                       "awaiting_review",
                                                       "done", "failed", "paused"]},
    "context": {"instruction_files": ["CLAUDE.md", "AGENTS.md", "CONTRIBUTING.md",
                                      ".cursorrules", ".github/copilot-instructions.md"],
                "max_instruction_chars": 12000},
}


class ConfigError(ValueError):
    pass


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


@dataclass
class Package:
    name: str
    root: str = "."
    triggers: list[str] = field(default_factory=lambda: ["**"])
    commands: dict[str, str] = field(default_factory=dict)
    ignore: list[str] = field(default_factory=list)

    def owns(self, path: str) -> bool:
        return any_match(path, self.triggers) and not any_match(path, self.ignore)


@dataclass
class Config:
    raw: dict[str, Any]
    source: str = ""

    # --- accessors -----------------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def version_hash(self) -> str:
        return sha(yaml.safe_dump(self.raw, sort_keys=True))

    @property
    def default_branch(self) -> str:
        return self.get("project.default_branch", "main")

    @property
    def packages(self) -> list[Package]:
        return [Package(name=p["name"], root=p.get("root", "."),
                        triggers=p.get("triggers") or ["**"], commands=p.get("commands") or {},
                        ignore=p.get("ignore") or [])
                for p in self.raw.get("packages", [])]

    @property
    def test_globs(self) -> list[str]:
        return self.get("tests.globs", DEFAULT_TEST_GLOBS)

    def is_test_path(self, path: str) -> bool:
        return any_match(path, self.test_globs)

    def role_model(self, role: str) -> dict[str, Any]:
        roles = self.get("models.roles", {})
        spec = roles.get(role) or roles.get("implementer") or {"model": "claude-opus-5-5"}
        return spec if isinstance(spec, dict) else {"model": spec}

    def gates_for(self, tier: int) -> dict[str, Any]:
        return self.get(f"gates.tier_{tier}") or self.get("gates.tier_2")

    def budget_for(self, tier: int) -> dict[str, Any]:
        return self.get(f"budgets.tier_{tier}") or self.get("budgets.tier_2")


def validate(raw: dict[str, Any]) -> list[str]:
    errors = []
    if raw.get("version") != 1:
        errors.append("version must be 1")
    names = set()
    for i, p in enumerate(raw.get("packages", [])):
        if "name" not in p:
            errors.append(f"packages[{i}] needs a name")
        elif p["name"] in names:
            errors.append(f"duplicate package name {p['name']}")
        names.add(p.get("name"))
        for cmd in (p.get("commands") or {}):
            if cmd not in {"setup", "lint", "typecheck", "build", "test", "coverage"}:
                errors.append(f"packages[{p.get('name')}]: unknown command kind '{cmd}'")
    for tier in range(4):
        g = raw.get("gates", {}).get(f"tier_{tier}")
        if not g:
            errors.append(f"gates.tier_{tier} missing")
            continue
        if g.get("merge") not in {"agent", "agent_after_approval", "human"}:
            errors.append(f"gates.tier_{tier}.merge must be agent | agent_after_approval | human")
    for rule in raw.get("tiers", {}).get("rules", []):
        if rule.get("tier") not in (0, 1, 2, 3) or not rule.get("paths"):
            errors.append(f"tier rule needs paths and tier 0-3: {rule}")
    mmt = raw.get("autonomy", {}).get("max_merge_tier", -1)
    if not isinstance(mmt, int) or mmt < -1 or mmt > 3:
        errors.append("autonomy.max_merge_tier must be -1..3")
    return errors


def load(path: str | Path | None = None, text: str | None = None) -> Config:
    if text is None:
        if path is None:
            raise ConfigError("no config path or text")
        text = Path(path).read_text()
    user = yaml.safe_load(text) or {}
    if not isinstance(user, dict):
        raise ConfigError("factory.yaml must be a mapping")
    raw = deep_merge(DEFAULTS, user)
    # Lists the user sets replace defaults wholesale, except tier rules, which append
    # so the safe docs rule is kept unless the user writes their own docs rule.
    errors = validate(raw)
    if errors:
        raise ConfigError("; ".join(errors))
    return Config(raw=raw, source=str(path or "<text>"))


def resolve(repo: Path, explicit: str | None, state_dir: Path) -> Config:
    """Config lookup order: --config, repo working tree, pending copy from `asf init`."""
    for candidate in [Path(explicit) if explicit else None, repo / "factory.yaml",
                      state_dir / "factory.yaml"]:
        if candidate and candidate.exists():
            return load(candidate)
    raise ConfigError(f"no factory.yaml for {repo}; run `asf init {repo}` first")
