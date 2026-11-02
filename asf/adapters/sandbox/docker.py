"""Worktree sandbox whose commands run inside a container.

Files live in a host worktree (so the tool broker can enforce path rules in
Python), but every command an agent or gate runs executes in a container with
the worktree mounted, no credentials, and by default no network.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Any

from ...util import ExecResult, run
from .local_worktree import LocalWorktreeProvider, WorktreeSandbox


def docker_command(root: Path, cwd: str, command: str, settings: dict[str, Any]) -> list[str]:
    docker = settings.get("docker", {})
    image = docker.get("image", "node:22-bookworm")
    network = docker.get("network", "none")
    args = ["docker", "run", "--rm", "-v", f"{root}:/work", "-w", f"/work/{cwd}".rstrip("/."),
            "--network", network, "--memory", docker.get("memory", "4g"),
            "--cpus", str(docker.get("cpus", 2)), "--pids-limit", "512",
            "-e", "CI=1", "-e", "NO_COLOR=1"]
    if hasattr(os, "getuid"):
        args += ["--user", f"{os.getuid()}:{os.getgid()}"]
    for key, value in (settings.get("env") or {}).items():
        args += ["-e", f"{key}={value}"]
    return [*args, image, "bash", "-lc", command]


class DockerSandbox(WorktreeSandbox):
    def exec(self, command: str, cwd: str = ".", timeout: float = 900) -> ExecResult:
        self._cwd(cwd)  # path check
        return run(docker_command(self.root, cwd, command, self.settings), timeout=timeout, shell=False)


class DockerProvider(LocalWorktreeProvider):
    kind = "docker"

    def _sandbox(self, root: Path, branch: str, base_sha: str) -> DockerSandbox:
        return DockerSandbox(self.ctx.repo, root, branch, base_sha, self.settings)

    def _prepare(self, root: Path) -> None:
        if not run("docker info", timeout=20).ok:
            raise RuntimeError("docker daemon is not reachable; start it or use execution.provider: local_worktree")
        settings = dict(self.settings)
        setup = settings.pop("setup", [])
        self.settings = {**settings, "setup": []}
        super()._prepare(root)
        self.settings["setup"] = setup
        for cmd in setup:
            res = run(docker_command(root, ".", cmd, {**self.settings, "docker": {
                **self.settings.get("docker", {}), "network": "bridge"}}), timeout=1800, shell=False)
            if not res.ok:
                raise RuntimeError(f"sandbox setup failed in container: {shlex.quote(cmd)}\n{res.tail(3000)}")
