"""Sandbox as a git worktree on a fresh branch.

This is the provider used on a developer machine and inside CI runners (where the
runner VM is the isolation boundary). Commands get a scrubbed environment with no
credentials. Heavy dependency folders (node_modules, .venv) are cloned
copy-on-write so the sandbox never writes into the developer's checkout.
"""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path
from typing import Any

from ...util import ExecResult, git, run, sandbox_env
from ..base import Sandbox, SandboxProvider

AUTHOR_ENV = {
    "GIT_AUTHOR_NAME": "Software Factory", "GIT_AUTHOR_EMAIL": "factory@asf.local",
    "GIT_COMMITTER_NAME": "Software Factory", "GIT_COMMITTER_EMAIL": "factory@asf.local",
}


class WorktreeSandbox(Sandbox):
    def __init__(self, repo: Path, root: Path, branch: str, base_sha: str, settings: dict[str, Any]):
        self.repo, self.root, self.branch, self.base_sha = repo, root, branch, base_sha
        self.settings = settings

    def _cwd(self, cwd: str) -> Path:
        target = (self.root / cwd).resolve()
        if not str(target).startswith(str(self.root.resolve())):
            raise PermissionError(f"cwd escapes sandbox: {cwd}")
        return target

    def exec(self, command: str, cwd: str = ".", timeout: float = 900) -> ExecResult:
        return run(command, cwd=self._cwd(cwd), timeout=timeout,
                   env=sandbox_env(self.settings.get("env")))

    def _git(self, *args: str, check: bool = True) -> str:
        return git(self.root, *args, check=check)

    @property
    def _pathspec(self) -> list[str]:
        # Cloned dependency folders must never show up as changes. Folders the repo already
        # ignores need no exclude (git rejects excluding an ignored path in `git add`).
        if not hasattr(self, "_spec"):
            excludes = []
            for d in self.settings.get("clone_dirs", []):
                ignored = run(["git", "-C", str(self.root), "check-ignore", "-q", d], shell=False).ok
                if not ignored:
                    excludes.append(f":(exclude){d}")
            self._spec = ["--", ".", *excludes]
        return self._spec

    def _stage_intent(self) -> None:
        self._git("add", "-A", "-N", *self._pathspec)

    def changed_files(self) -> list[str]:
        self._stage_intent()
        out = self._git("diff", "--name-only", self.base_sha, *self._pathspec)
        return sorted({line.strip() for line in out.splitlines() if line.strip()})

    def diff(self, paths: list[str] | None = None) -> str:
        self._stage_intent()
        if paths:
            return self._git("diff", self.base_sha, "--", *paths)
        return self._git("diff", self.base_sha, *self._pathspec)

    def numstat(self) -> list[tuple[int, int, str]]:
        self._stage_intent()
        rows = []
        for line in self._git("diff", "--numstat", self.base_sha, *self._pathspec).splitlines():
            added, deleted, path = line.split("\t", 2)
            rows.append((int(added) if added != "-" else 0, int(deleted) if deleted != "-" else 0, path))
        return rows

    def file_at_base(self, path: str) -> str | None:
        return self.file_at(self.base_sha, path)

    def file_at(self, rev: str, path: str) -> str | None:
        res = run(["git", "-C", str(self.root), "show", f"{rev}:{path}"], shell=False)
        return res.stdout if res.ok else None

    def changed_files_at(self, rev: str) -> list[str]:
        out = self._git("diff", "--name-only", self.base_sha, rev, *self._pathspec)
        return sorted({line.strip() for line in out.splitlines() if line.strip()})

    def commit(self, message: str) -> str:
        self._git("add", "-A", *self._pathspec)
        res = run(["git", "-C", str(self.root), "commit", "-q", "--no-verify", "-m", message],
                  shell=False, env={**os.environ, **AUTHOR_ENV})
        if not res.ok and "nothing to commit" not in res.stdout + res.stderr:
            raise RuntimeError(res.tail(2000))
        return self._git("rev-parse", "HEAD").strip()

    def reset(self, to: str | None = None) -> None:
        self._git("reset", "-q", "--hard", to or self.base_sha)
        self._git("clean", "-fdq", *self._pathspec)

    def uncommitted_files(self) -> list[str]:
        self._stage_intent()
        out = self._git("diff", "--name-only", "HEAD", *self._pathspec)
        return sorted({line.strip() for line in out.splitlines() if line.strip()})

    def destroy(self) -> None:
        git(self.repo, "worktree", "remove", "--force", str(self.root), check=False)
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        git(self.repo, "worktree", "prune", check=False)


def clone_dir(src: Path, dst: Path, mode: str = "copy") -> str:
    """Copy-on-write clone where the filesystem supports it, symlink as last resort."""
    if dst.exists() or dst.is_symlink():
        return "exists"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if mode == "copy":
        flags = ["-cR"] if platform.system() == "Darwin" else ["-R", "--reflink=auto"]
        if run(["cp", *flags, str(src), str(dst)], shell=False, timeout=900).ok:
            return "cloned"
    os.symlink(src, dst)
    return "symlinked"


LOCKFILES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock", "poetry.lock",
             "uv.lock", "Pipfile.lock", "requirements.txt", "requirements-dev.txt", "Gemfile.lock")


def lock_key(repo: Path, rel: str) -> str:
    """Hash of the lockfiles next to a dependency folder: same lockfile, same cache."""
    import hashlib
    parent = (repo / rel).parent
    h = hashlib.sha256(rel.encode())
    for name in LOCKFILES:
        f = parent / name
        if f.exists():
            h.update(name.encode() + f.read_bytes())
    return h.hexdigest()[:12]


class LocalWorktreeProvider(SandboxProvider):
    kind = "local_worktree"

    @property
    def base_dir(self) -> Path:
        return self.ctx.state_dir / "sandboxes"

    def create(self, job_id: str, base_ref: str, branch: str) -> Sandbox:
        root = self.base_dir / job_id
        base_sha = git(self.ctx.repo, "rev-parse", base_ref).strip()
        if not root.exists():
            root.parent.mkdir(parents=True, exist_ok=True)
            exists = run(["git", "-C", str(self.ctx.repo), "rev-parse", "--verify", f"refs/heads/{branch}"],
                         shell=False).ok
            args = ["worktree", "add", str(root), branch] if exists else \
                ["worktree", "add", "-b", branch, str(root), base_sha]
            git(self.ctx.repo, *args)
        self._prepare(root)
        return self._sandbox(root, branch, base_sha)

    def _sandbox(self, root: Path, branch: str, base_sha: str) -> Sandbox:
        return WorktreeSandbox(self.ctx.repo, root, branch, base_sha, self.settings)

    def _prepare(self, root: Path) -> None:
        mode = self.settings.get("clone_mode", "shared")
        for rel in self.settings.get("clone_dirs", []):
            src = self.ctx.repo / rel
            if not src.exists():
                continue
            if mode == "shared":
                # One copy-on-write clone per lockfile version, shared by all sandboxes.
                # The developer's own folder is never written to.
                cache = (self.ctx.state_dir / "deps" / f"{rel.replace('/', '__')}-{lock_key(self.ctx.repo, rel)}"
                         / Path(rel).name)
                if not cache.exists():
                    tmp = cache.parent / (cache.name + ".partial")
                    shutil.rmtree(tmp, ignore_errors=True)
                    clone_dir(src, tmp, "copy")
                    tmp.rename(cache)
                dst = root / rel
                if not (dst.exists() or dst.is_symlink()):
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    os.symlink(cache, dst)
            else:
                clone_dir(src, root / rel, mode)
        marker = root.parent / f"{root.name}.setup-done"
        if not marker.exists():
            for cmd in self.settings.get("setup", []):
                res = run(cmd, cwd=root, timeout=1800, env=sandbox_env(self.settings.get("env")))
                if not res.ok:
                    raise RuntimeError(f"sandbox setup failed: {cmd}\n{res.tail(3000)}")
            marker.write_text("ok")

    def reopen(self, info: dict[str, Any]) -> Sandbox:
        root = Path(info["root"])
        if not root.exists():
            return self.create(root.name, info["base_sha"], info["branch"])
        return self._sandbox(root, info["branch"], info["base_sha"])

    def base_checkout(self, base_sha: str) -> Sandbox:
        root = self.base_dir / f"base-{base_sha[:12]}"
        if not root.exists():
            root.parent.mkdir(parents=True, exist_ok=True)
            git(self.ctx.repo, "worktree", "add", "--detach", str(root), base_sha)
        self._prepare(root)
        return self._sandbox(root, "(detached)", base_sha)
