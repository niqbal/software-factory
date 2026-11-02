"""Delivery to GitLab with the `glab` CLI: push and open a (draft) merge request."""

from __future__ import annotations

import tempfile
from typing import Any

from ...models import WorkItem
from ...util import run
from ..base import DeliveryAdapter, Sandbox


class GitLabDelivery(DeliveryAdapter):
    kind = "gitlab"

    def _run(self, args: list[str]):
        runner = self.ctx.runner or run
        res = runner(args, cwd=self.ctx.repo, shell=False, timeout=180)
        if not res.ok:
            raise RuntimeError(f"{' '.join(args[:3])} failed: {res.tail(1500)}")
        return res

    def deliver(self, job: dict[str, Any], item: WorkItem, sandbox: Sandbox,
                report_md: str, draft: bool) -> dict[str, Any]:
        self._run(["git", "push", "-u", self.settings.get("remote", "origin"), f"refs/heads/{sandbox.branch}"])
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write(report_md)
        title = ("Draft: " if draft else "") + item.title
        res = self._run(["glab", "mr", "create", "--source-branch", sandbox.branch,
                         "--target-branch", self.ctx.config.default_branch, "--title", title,
                         "--description", report_md, "--yes"])
        return {"kind": self.kind, "branch": sandbox.branch, "url": res.stdout.strip().splitlines()[-1],
                "draft": draft}

    def merge(self, job: dict[str, Any], delivery: dict[str, Any]) -> dict[str, Any]:
        self._run(["glab", "mr", "merge", delivery["branch"], "--yes", "--squash"])
        return {"url": delivery.get("url"), "state": "merged"}
