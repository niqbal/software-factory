"""Delivery to a local branch. The default for shadow mode and laptops.

The sandbox worktree shares the repository's object store, so the factory's
commit is already on the branch in the user's repository. Nothing is pushed.
"""

from __future__ import annotations

from typing import Any

from ...models import WorkItem
from ...util import git, run
from ..base import DeliveryAdapter, Sandbox


class LocalGitDelivery(DeliveryAdapter):
    kind = "local_git"

    def deliver(self, job: dict[str, Any], item: WorkItem, sandbox: Sandbox,
                report_md: str, draft: bool) -> dict[str, Any]:
        sha = git(self.ctx.repo, "rev-parse", f"refs/heads/{sandbox.branch}").strip()
        return {"kind": self.kind, "branch": sandbox.branch, "commit": sha,
                "report": job["data"].get("report"), "draft": draft,
                "review_command": f"git -C {self.ctx.repo} diff {sandbox.base_sha[:12]}..{sandbox.branch}"}

    def merge(self, job: dict[str, Any], delivery: dict[str, Any]) -> dict[str, Any]:
        repo = self.ctx.repo
        target = self.ctx.config.default_branch
        current = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        if current != target:
            raise RuntimeError(f"refusing to merge: {repo} is on {current}, not {target}")
        if git(repo, "status", "--porcelain").strip():
            raise RuntimeError("refusing to merge: the working tree has uncommitted changes")
        res = run(["git", "-C", str(repo), "merge", "--no-ff", "--no-edit", delivery["branch"]], shell=False)
        if not res.ok:
            run(["git", "-C", str(repo), "merge", "--abort"], shell=False)
            raise RuntimeError(f"merge failed: {res.tail(1500)}")
        return {"merged_into": target, "commit": git(repo, "rev-parse", "HEAD").strip()}
