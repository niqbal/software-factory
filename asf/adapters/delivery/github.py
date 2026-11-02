"""Delivery to GitHub: push the branch and open a (draft) pull request with `gh`.

Uses the GitHub CLI's existing authentication, or GH_TOKEN in CI. Merge uses
`gh pr merge`, which respects branch protection and required checks.
"""

from __future__ import annotations

import json
import tempfile
from typing import Any

from ...models import WorkItem
from ...util import ExecResult, run
from ..base import DeliveryAdapter, Sandbox


class GitHubDelivery(DeliveryAdapter):
    kind = "github"

    def _run(self, args: list[str]) -> ExecResult:
        runner = self.ctx.runner or run
        res = runner(args, cwd=self.ctx.repo, shell=False, timeout=180)
        if not res.ok:
            raise RuntimeError(f"{' '.join(args[:3])} failed: {res.tail(1500)}")
        return res

    def deliver(self, job: dict[str, Any], item: WorkItem, sandbox: Sandbox,
                report_md: str, draft: bool) -> dict[str, Any]:
        remote = self.settings.get("remote", "origin")
        self._run(["git", "push", "-u", remote, f"refs/heads/{sandbox.branch}"])
        labels = self.settings.get("labels", ["factory"])
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write(report_md)
            body = fh.name
        args = ["gh", "pr", "create", "--head", sandbox.branch,
                "--base", self.ctx.config.default_branch, "--title", item.title, "--body-file", body]
        if draft:
            args.append("--draft")
        for label in labels:
            args += ["--label", label]
        res = self._run(args)
        url = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else ""
        return {"kind": self.kind, "branch": sandbox.branch, "url": url, "draft": draft}

    def merge(self, job: dict[str, Any], delivery: dict[str, Any]) -> dict[str, Any]:
        url = delivery["url"]
        self._run(["gh", "pr", "ready", url])
        self._run(["gh", "pr", "merge", url, f"--{self.settings.get('merge_method', 'squash')}",
                   "--delete-branch"])
        info = json.loads(self._run(["gh", "pr", "view", url, "--json", "mergeCommit,state"]).stdout)
        return {"url": url, "state": info.get("state"), "commit": (info.get("mergeCommit") or {}).get("oid")}
