"""Verification of a checkout the factory does not own.

The factory's gates normally run inside a sandbox it created for a job. This module runs
the same gates over a checkout someone else owns: a developer's working tree, or the one
an interactive coding agent has just written into. It never writes there. No commit, no
reset, and not even a change to the git index: ``git add -A -N``, which the sandbox uses
so untracked files show up in the diff, is applied to a scratch index file instead.

Two things make the verdict worth more than the writing agent's own account of its work:
the gates are deterministic, and failing commands are re-run on the base commit, so a
failure that was already there is reported as pre-existing instead of blamed on the change.

A repo with no ``factory.yaml`` still works: the layout is detected the way ``asf init``
drafts one, in memory. This is the engine behind the asf MCP server (``asf/mcp_server.py``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

from . import config as config_mod
from .adapters import base as adapters
from .adapters.base import AdapterContext, Sandbox
from .adapters.sandbox.local_worktree import LocalWorktreeProvider
from .config import Config
from .gates import COMMAND_GATES, GateRunner
from .models import GateResult
from .util import ExecResult, run, sandbox_env, sha

# Gates that only read the diff: no project command runs, so they answer in milliseconds.
STATIC_GATES = ("secret_scan", "diff_size", "test_integrity", "dependency_policy")


class VerifyError(RuntimeError):
    pass


class ReadOnlySandbox(Sandbox):
    """A checkout the factory does not own, read through the Sandbox interface.

    Reading works (diff, changed files, file at base, exec). Writing raises: this points
    at someone else's working tree. ``host_shell`` is False so a native agent session is
    never started in it either; a reviewer here goes through the broker's read-only tools.
    """

    host_shell = False

    def __init__(self, repo: Path, base_sha: str, branch: str, settings: dict[str, Any],
                 index_dir: Path):
        self.repo = self.root = repo.resolve()
        self.base_sha, self.branch, self.settings = base_sha, branch, settings
        self._index = index_dir / f"index-{sha(str(self.root), 8)}"
        self._timeout = float(settings.get("command_timeout_s", 900))

    # ----------------------------------------------------------------- reading --
    def _git(self, *args: str, check: bool = True) -> str:
        """git with a scratch index, so staging never touches the index in use."""
        env = {**os.environ, "GIT_INDEX_FILE": str(self._index)}
        res = run(["git", "-C", str(self.root), *args], shell=False, env=env)
        if check and not res.ok:
            raise VerifyError(f"git {' '.join(args)} failed: {res.tail(1000)}")
        return res.stdout

    @property
    def _pathspec(self) -> list[str]:
        # Cloned dependency folders must not read as changes. A folder the repo already
        # ignores needs no exclude (git rejects excluding an ignored path in `git add`).
        if not hasattr(self, "_spec"):
            excludes = []
            for d in self.settings.get("clone_dirs", []):
                ignored = run(["git", "-C", str(self.root), "check-ignore", "-q", d], shell=False).ok
                if not ignored:
                    excludes.append(f":(exclude){d}")
            self._spec = ["--", ".", *excludes]
        return self._spec

    def _stage(self) -> None:
        """Intent-to-add the whole tree into the scratch index, so untracked files appear in
        the diff. Re-done on every read: the agent keeps editing between our calls."""
        self._index.parent.mkdir(parents=True, exist_ok=True)
        self._index.unlink(missing_ok=True)
        self._git("read-tree", "HEAD")
        self._git("add", "-A", "-N", *self._pathspec)

    def changed_files(self) -> list[str]:
        self._stage()
        out = self._git("diff", "--name-only", self.base_sha, *self._pathspec)
        return sorted({line.strip() for line in out.splitlines() if line.strip()})

    def diff(self, paths: list[str] | None = None) -> str:
        self._stage()
        if paths:
            return self._git("diff", self.base_sha, "--", *paths)
        return self._git("diff", self.base_sha, *self._pathspec)

    def numstat(self) -> list[tuple[int, int, str]]:
        self._stage()
        rows = []
        for line in self._git("diff", "--numstat", self.base_sha, *self._pathspec).splitlines():
            added, deleted, path = line.split("\t", 2)
            rows.append((int(added) if added != "-" else 0, int(deleted) if deleted != "-" else 0, path))
        return rows

    def file_at_base(self, path: str) -> str | None:
        res = run(["git", "-C", str(self.root), "show", f"{self.base_sha}:{path}"], shell=False)
        return res.stdout if res.ok else None

    def exec(self, command: str, cwd: str = ".", timeout: float | None = None) -> ExecResult:
        target = (self.root / cwd).resolve()
        if not str(target).startswith(str(self.root)):
            raise PermissionError(f"cwd escapes the checkout: {cwd}")
        return run(command, cwd=target, timeout=timeout or self._timeout,
                   env=sandbox_env(self.settings.get("env")))

    # ----------------------------------------------------------------- writing --
    def _refuse(self, what: str) -> None:
        raise VerifyError(f"the verifier never {what} in a checkout it does not own ({self.root})")

    def commit(self, message: str) -> str:
        self._refuse("commits")
        return ""

    def reset(self, to: str | None = None) -> None:
        self._refuse("resets")

    def destroy(self) -> None:
        # Nothing of ours to remove but the scratch index.
        self._index.unlink(missing_ok=True)


# --------------------------------------------------------------------------- setup --

def _rev(repo: Path, ref: str) -> str:
    res = run(["git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}"], shell=False)
    if not res.ok:
        raise VerifyError(f"not a commit in {repo}: {ref}")
    return res.stdout.strip()


def resolve_base(repo: Path, cfg: Config, base: str | None = None) -> str:
    """What the change is measured against.

    Default: the merge base with the default branch, so a feature branch is verified whole.
    On the default branch itself that is HEAD, so only uncommitted work is verified.
    """
    if base:
        return _rev(repo, base)
    default = cfg.default_branch
    for ref in (f"origin/{default}", default):
        if run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", ref], shell=False).ok:
            mb = run(["git", "-C", str(repo), "merge-base", "HEAD", ref], shell=False)
            if mb.ok and mb.stdout.strip():
                return mb.stdout.strip()
    return _rev(repo, "HEAD")


def state_dir_for(repo: Path) -> Path:
    home = Path(os.environ.get("ASF_HOME", "~/.asf")).expanduser()
    return home / f"{repo.name}-{sha(str(repo), 6)}"


def config_for(repo: Path, explicit: str | None, state_dir: Path) -> tuple[Config, bool]:
    """The repo's factory.yaml when it has one, else a config detected from its layout."""
    try:
        return config_mod.resolve(repo, explicit, state_dir), True
    except config_mod.ConfigError:
        from .setup_agent import detect
        return config_mod.load(text=yaml.safe_dump(detect(repo))), False


def _result(r: GateResult, log_chars: int) -> dict[str, Any]:
    out = {"gate": r.name, "package": r.package, "status": r.status, "detail": r.detail}
    if r.log and r.status == "fail" and log_chars:
        out["log"] = r.log[-log_chars:]
    return out


def verdict(results: Iterable[GateResult]) -> dict[str, Any]:
    results = list(results)
    failed = [r for r in results if r.blocking]
    return {"verdict": "blocked" if failed else "clean",
            "failed": len(failed),
            "passed": sum(1 for r in results if r.status == "pass"),
            "preexisting": sum(1 for r in results if r.status == "preexisting"),
            "skipped": sum(1 for r in results if r.status == "skipped")}


@dataclass
class Verifier:
    """Gates and a cold review over one checkout."""

    repo: Path
    cfg: Config
    state_dir: Path
    configured: bool = True

    @classmethod
    def open(cls, repo: str | Path, config: str | None = None,
             state_dir: str | Path | None = None) -> "Verifier":
        path = Path(repo).expanduser().resolve()
        if not run(["git", "-C", str(path), "rev-parse", "--git-dir"], shell=False).ok:
            raise VerifyError(f"not a git repository: {path}")
        sd = Path(state_dir).expanduser() if state_dir else state_dir_for(path)
        cfg, configured = config_for(path, config, sd)
        return cls(path, cfg, sd, configured)

    # ------------------------------------------------------------------ plumbing --
    @property
    def _execution(self) -> dict[str, Any]:
        return self.cfg.get("execution") or {}

    def sandbox(self, base: str | None = None) -> ReadOnlySandbox:
        branch = run(["git", "-C", str(self.repo), "rev-parse", "--abbrev-ref", "HEAD"],
                     shell=False).stdout.strip() or "(detached)"
        return ReadOnlySandbox(self.repo, resolve_base(self.repo, self.cfg, base), branch,
                               self._execution, self.state_dir / "verify")

    def _base_sandbox(self, base_sha: str) -> Callable[[], Sandbox]:
        ctx = AdapterContext(repo=self.repo, state_dir=self.state_dir, config=self.cfg)
        provider = LocalWorktreeProvider(self._execution, ctx)
        return lambda: provider.base_checkout(base_sha)

    def _gate_names(self, names: Iterable[str] | None, tier: int) -> list[str]:
        if names:
            return list(names)
        return list((self.cfg.gates_for(tier) or {}).get("deterministic") or STATIC_GATES)

    # ----------------------------------------------------------------------- api --
    def checks(self, tier: int = 2, base: str | None = None) -> dict[str, Any]:
        """What can be verified here, without running anything."""
        sb = self.sandbox(base)
        changed = sb.changed_files()
        tier_cfg = self.cfg.gates_for(tier) or {}
        packages = []
        for pkg in self.cfg.packages:
            owned = [c for c in changed if pkg.owns(c)]
            if owned:
                packages.append({"package": pkg.name, "root": pkg.root, "changed_files": len(owned),
                                 "commands": {k: v for k, v in pkg.commands.items()
                                              if k in COMMAND_GATES}})
        return {"repo": str(self.repo), "branch": sb.branch, "base_sha": sb.base_sha[:12],
                "config": self.cfg.source if self.configured else "detected (no factory.yaml)",
                "tier": tier, "changed_files": changed,
                "gates": self._gate_names(None, tier),
                "packages_touched": packages,
                "reviews": list(tier_cfg.get("llm") or [])}

    def gates(self, names: Iterable[str] | None = None, tier: int = 2, base: str | None = None,
              compare_base: bool = True, log_chars: int = 3000) -> dict[str, Any]:
        """Run the gates. Command gates run only for packages the diff touches."""
        sb = self.sandbox(base)
        wanted = self._gate_names(names, tier)
        unknown = [n for n in wanted if n not in COMMAND_GATES and n not in STATIC_GATES]
        runs_commands = any(n in COMMAND_GATES for n in wanted)
        base_sb = self._base_sandbox(sb.base_sha) if compare_base and runs_commands else None
        results = GateRunner(self.cfg, sb, tier, self.state_dir, base_sandbox=base_sb).run(wanted)
        out = {"repo": str(self.repo), "branch": sb.branch, "base_sha": sb.base_sha[:12], "tier": tier,
               "changed_files": sb.changed_files(), "compared_with_base": base_sb is not None,
               "results": [_result(r, log_chars) for r in results], **verdict(results)}
        if unknown:
            out["unknown_gates"] = unknown
        return out

    def static(self, tier: int = 2, base: str | None = None) -> dict[str, Any]:
        """The read-only gates alone: no project command is executed."""
        return self.gates(names=STATIC_GATES, tier=tier, base=base, compare_base=False)

    def review(self, intent: str = "", tier: int = 2, base: str | None = None,
               role: str = "reviewer", gates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """A cold read of the diff by a model that did not write it.

        The reviewer sees the diff and the repository through the broker's read-only tools
        and cannot write; it reports a verdict and findings with severities.
        """
        from .agents.roles import PromptBuilder, run_agent
        from .broker import ToolBroker

        if role not in ("reviewer", "security"):
            raise VerifyError(f"not a review role: {role}")
        sb = self.sandbox(base)
        if not sb.changed_files():
            return {"role": role, "verdict": "approve", "summary": "nothing changed against the base",
                    "findings": [], "base_sha": sb.base_sha[:12]}
        models = self.cfg.get("models") or {}
        ctx = AdapterContext(repo=self.repo, state_dir=self.state_dir, config=self.cfg)
        provider = adapters.load("models", models.get("provider", "anthropic"))(models, ctx)
        broker = ToolBroker(sb, self.cfg, role, tier)
        spent: dict[str, float] = {"usd": 0.0, "turns": 0}

        def charge(_role: str, turn: Any) -> None:
            spent["usd"] += getattr(turn, "cost_usd", 0.0) or 0.0
            spent["turns"] += 1

        gate_lines = "\n".join(f"- {g['gate']}{'/' + g['package'] if g.get('package') else ''}: "
                               f"{g['status']} ({g['detail']})" for g in (gates or []))
        task = ("Review the change in this checkout against its base commit "
                f"{sb.base_sha[:12]}. Read it with show_diff.\n\n"
                f"<intent>\n{intent.strip() or '(the author stated no intent)'}\n</intent>\n")
        if gate_lines:
            task += f"\nDeterministic gates already run:\n{gate_lines}\n"
        task += ("\nYou did not write this change and cannot edit it. Judge what is there, not what "
                 "you would have written. Call submit_review when you are done.")
        res = run_agent(provider, self.cfg, role, PromptBuilder(self.cfg, self.repo).system(role, tier),
                        task, broker, charge)
        data = res.data or {}
        return {"role": role, "base_sha": sb.base_sha[:12], "ok": res.ok,
                "verdict": data.get("verdict", "unknown"),
                "summary": data.get("summary") or res.summary,
                "findings": data.get("findings", []),
                "turns": res.turns, "cost_usd": round(res.cost_usd, 4),
                "model": (self.cfg.role_model(role) or {}).get("model", "")}
