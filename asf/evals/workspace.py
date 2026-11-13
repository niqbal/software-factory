"""Clean checkouts for eval cases, and the throwaway project's config.

Each run gets its own clone holding only the history up to the base commit: the reference
solution and its tests are not in it, so neither the factory nor the interactive baseline can
find them with `git log --all`. Clones come from a mirror cache under $ASF_HOME/evals/mirrors,
fetched by commit id, so a product repository is only ever read, never written to. The clone
has no remote: nothing an eval does can be pushed.
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Any

import yaml

from .. import config as config_mod
from .. import setup_wizard
from ..adapters.sandbox.local_worktree import AUTHOR_ENV, clone_dir
from ..projects import asf_home
from ..util import git, run, sandbox_env, sha
from .cases import Case, EvalError

BRANCH = "main"
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

# What every eval project is forced to, whatever the case or variant says: shadow mode, a local
# branch as delivery, no outside intake, notifications or deploys. Evals never merge or push.
FORCED: dict[str, Any] = {
    "autonomy": {"shadow": True, "max_merge_tier": -1},
    "delivery": {"provider": "local_git", "draft": True},
    "intake": {"local": {"enabled": True}},
    "notifications": {"providers": []},
    "observe": {"provider": "none"},
    "deploy": {"steps": []},
}


def eval_home() -> Path:
    return asf_home() / "evals"


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def mirror(case: Case) -> Path:
    """A bare mirror of the case's repository that has both the base and reference commits."""
    src = str(case.local_repo) if case.local_repo else case.repo
    name = Path(src.rstrip("/")).name.removesuffix(".git") or "repo"
    dest = eval_home() / "mirrors" / f"{name}-{sha(src, 8)}.git"
    with _lock(str(dest)):
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".partial")
            shutil.rmtree(tmp, ignore_errors=True)
            res = run(["git", "clone", "-q", "--mirror", src, str(tmp)], shell=False, timeout=1800)
            if not res.ok:
                raise EvalError(f"cannot clone {src}: {res.tail(800)}")
            git(tmp, "config", "uploadpack.allowAnySHA1InWant", "true")
            tmp.rename(dest)
        missing = [c for c in (case.base, case.reference) if not has_commit(dest, c)]
        if missing:
            git(dest, "fetch", "-q", "--prune", "origin", timeout=1800, check=False)
            missing = [c for c in (case.base, case.reference) if not has_commit(dest, c)]
        if missing:
            raise EvalError(f"case {case.id}: commit(s) {', '.join(missing)} not found in {src}")
    return dest


def has_commit(repo: Path, ref: str) -> bool:
    return run(["git", "-C", str(repo), "cat-file", "-e", f"{ref}^{{commit}}"], shell=False).ok


def resolve(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", f"{ref}^{{commit}}").strip()


def checkout(case: Case, dest: Path) -> tuple[Path, str]:
    """A fresh clone at the base commit on branch `main`, with only the base's history.

    Returns (checkout, base sha)."""
    m = mirror(case)
    base = resolve(m, case.base)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    git(dest, "init", "-q", "-b", BRANCH)
    git(dest, "fetch", "-q", "--no-tags", "--update-head-ok", f"file://{m}", f"{base}:refs/heads/{BRANCH}", timeout=1800)
    git(dest, "checkout", "-q", BRANCH)
    git(dest, "config", "user.name", AUTHOR_ENV["GIT_AUTHOR_NAME"])
    git(dest, "config", "user.email", AUTHOR_ENV["GIT_AUTHOR_EMAIL"])
    return dest, base


def prepare(case: Case, root: Path, cfg_raw: dict[str, Any]) -> list[str]:
    """Dependency folders and setup commands, so the checkout can run its tests.

    `execution.clone_dirs` (node_modules, .venv) are cloned copy-on-write from `deps_from`
    (default: the case's local repository). Then the case's `setup` commands run. Returns notes."""
    notes: list[str] = []
    src = Path(case.deps_from).expanduser() if case.deps_from else case.local_repo
    for rel in (cfg_raw.get("execution") or {}).get("clone_dirs") or []:
        if src and (src / rel).exists():
            notes.append(f"{rel}: {clone_dir(src / rel, root / rel, 'copy')} from {src}")
        else:
            notes.append(f"{rel}: not found in {src}; add a setup command that creates it")
    env = sandbox_env((cfg_raw.get("execution") or {}).get("env"))
    for cmd in case.setup:
        res = run(cmd, cwd=root, timeout=1800, env=env)
        if not res.ok:
            raise EvalError(f"case {case.id}: setup failed: {cmd}\n{res.tail(2000)}")
        notes.append(f"setup: {cmd}")
    return notes


def base_config(case: Case, root: Path) -> tuple[dict[str, Any], str]:
    """The case's factory.yaml before the variant: (raw user mapping, where it came from).

    In order: the case's `config`; factory.yaml in the checkout at the base commit; the local
    repository's current factory.yaml; the registered project's pending draft; else a draft
    from the setup wizard's detection, as `asf setup add` would write."""
    if isinstance(case.config, dict):
        return dict(case.config), "case"
    if isinstance(case.config, str) and case.config:
        p = Path(case.config).expanduser()
        if not p.is_absolute() and case.path:
            p = case.path.parent / p
        return yaml.safe_load(p.read_text()) or {}, str(p)
    candidates = [root / "factory.yaml"]
    if case.local_repo:
        candidates += [case.local_repo / "factory.yaml", setup_wizard.state_dir(case.local_repo) / "factory.yaml"]
    for p in candidates:
        if p.exists():
            return yaml.safe_load(p.read_text()) or {}, str(p)
    d = setup_wizard.draft(root, {"backend": "auto"})
    return yaml.safe_load(d["yaml"]) or {}, "setup wizard draft"


def eval_config(raw: dict[str, Any]) -> config_mod.Config:
    """Validate a variant-applied config with the eval's forced settings on top."""
    forced = config_mod.deep_merge(raw, {k: v for k, v in FORCED.items() if k != "intake"})
    forced["intake"] = dict(FORCED["intake"])  # replace: no GitHub, Jira, ... intake in an eval
    forced.setdefault("project", {})
    forced["project"] = {**forced["project"], "default_branch": BRANCH, "remote": ""}
    return config_mod.load(text=yaml.safe_dump(forced, sort_keys=False))


def pathspec(cfg: config_mod.Config) -> list[str]:
    """Excludes for dependency folders cloned into a checkout, as the worktree sandbox uses."""
    return [f":(exclude){d}" for d in cfg.get("execution.clone_dirs", []) or []]


def tree_patch(root: Path, base: str, cfg: config_mod.Config, ref: str | None = None) -> str:
    """A binary patch from base to `ref`, or to the working tree (untracked files included)."""
    spec = ["--", ".", *[e for e in pathspec(cfg) if _not_ignored(root, e)]]
    if ref:
        return git(root, "diff", "--binary", base, ref, *spec)
    git(root, "add", "-A", "-N", *spec)
    return git(root, "diff", "--binary", base, *spec)


def _not_ignored(root: Path, exclude: str) -> bool:
    rel = exclude.split(")", 1)[1]
    return not run(["git", "-C", str(root), "check-ignore", "-q", rel], shell=False).ok


def reset(root: Path, base: str, cfg: config_mod.Config) -> None:
    """Back to the base commit, keeping cloned dependency folders."""
    git(root, "checkout", "-q", "--detach", base)
    git(root, "reset", "-q", "--hard", base)
    args = ["clean", "-fdq"]
    for d in cfg.get("execution.clone_dirs", []) or []:
        args += ["-e", d]
    git(root, *args)
