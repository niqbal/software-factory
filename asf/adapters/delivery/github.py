"""Delivery to GitHub: push the branch and open a (draft) pull request with `gh`.

Uses the GitHub CLI's existing authentication, or GH_TOKEN in CI. Merge uses
`gh pr merge`, which respects branch protection and required checks.

Delivering again (after `asf revise`) pushes the new commit and updates the open
pull request instead of opening a second one. The adapter also reads a pull
request's comments, so review on GitHub can drive revisions (`asf reviews`).
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from ...models import WorkItem
from ...util import ExecResult, run
from ..base import DeliveryAdapter, Sandbox

# Comment authors whose words may steer the factory. Everyone else is ignored and logged:
# a pull request comment is untrusted input like any issue body.
TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")


def pr_body(item: WorkItem, report_md: str) -> str:
    """The report, plus a closing keyword so merging the PR closes the GitHub issue."""
    body = report_md.rstrip() + "\n"
    if item.source_kind == "github_issues" and item.external_id.isdigit():
        body += f"\nFixes #{item.external_id}\n"
    return body


class GitHubDelivery(DeliveryAdapter):
    kind = "github"

    def _run(self, args: list[str]) -> ExecResult:
        runner = self.ctx.runner or run
        res = runner(args, cwd=self.ctx.repo, shell=False, timeout=180)
        if not res.ok:
            raise RuntimeError(f"{' '.join(args[:3])} failed: {res.tail(1500)}")
        return res

    def _json(self, args: list[str]) -> Any:
        out = self._run(args).stdout.strip()
        return json.loads(out) if out else None

    def _with_file(self, text: str, build: Any) -> ExecResult:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write(text)
            path = fh.name
        try:
            return self._run(build(path))
        finally:
            os.unlink(path)

    def open_pr(self, branch: str) -> dict[str, Any] | None:
        rows = self._json(["gh", "pr", "list", "--head", branch, "--state", "open",
                           "--json", "number,url"]) or []
        return rows[0] if rows else None

    def deliver(self, job: dict[str, Any], item: WorkItem, sandbox: Sandbox,
                report_md: str, draft: bool) -> dict[str, Any]:
        remote = self.settings.get("remote", "origin")
        branch = sandbox.branch
        self._run(["git", "push", "-u", remote, f"refs/heads/{branch}:refs/heads/{branch}"])
        body = pr_body(item, report_md)
        existing = self.open_pr(branch)
        if existing:
            # REST rather than `gh pr edit`: older gh versions query classic Projects there, which
            # GitHub has removed, so the edit fails even though nothing about projects is changed.
            self._with_file(body, lambda p: ["gh", "api", "-X", "PATCH",
                                             f"repos/{{owner}}/{{repo}}/pulls/{existing['number']}",
                                             "-F", f"body=@{p}", "--silent"])
            url, number, updated = existing["url"], existing["number"], True
        else:
            def create(p: str) -> list[str]:
                args = ["gh", "pr", "create", "--head", branch, "--base", self.ctx.config.default_branch,
                        "--title", item.title, "--body-file", p]
                if draft:
                    args.append("--draft")
                for label in self.settings.get("labels", ["factory"]):
                    args += ["--label", label]
                return args
            res = self._with_file(body, create)
            url = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else ""
            number = int(url.rstrip("/").rsplit("/", 1)[-1]) if url.rstrip("/").rsplit("/", 1)[-1].isdigit() \
                else None
            updated = False
        return {"kind": self.kind, "branch": branch, "url": url, "number": number, "draft": draft,
                "updated": updated}

    def merge(self, job: dict[str, Any], delivery: dict[str, Any]) -> dict[str, Any]:
        url = delivery["url"]
        self._run(["gh", "pr", "ready", url])
        self._run(["gh", "pr", "merge", url, f"--{self.settings.get('merge_method', 'squash')}",
                   "--delete-branch"])
        info = json.loads(self._run(["gh", "pr", "view", url, "--json", "mergeCommit,state"]).stdout)
        return {"url": url, "state": info.get("state"), "commit": (info.get("mergeCommit") or {}).get("oid")}

    # ------------------------------------------------------------ review loop --
    def pr_state(self, number: int) -> str:
        info = self._json(["gh", "pr", "view", str(number), "--json", "state"]) or {}
        return str(info.get("state", "")).upper()

    def comments(self, number: int) -> list[dict[str, Any]]:
        """Every comment on a pull request, oldest first, in one shape:
        {id, kind: comment|review|line, author, association, body, created_at, path, line}."""
        base = "repos/{owner}/{repo}"
        out: list[dict[str, Any]] = []
        sources = [("comment", f"{base}/issues/{number}/comments"),
                   ("review", f"{base}/pulls/{number}/reviews"),
                   ("line", f"{base}/pulls/{number}/comments")]
        for kind, path in sources:
            for c in self._json(["gh", "api", f"{path}?per_page=100"]) or []:
                body = (c.get("body") or "").strip()
                if kind == "review" and not body:
                    continue  # an approval or a review whose content is all line comments
                out.append({"id": f"{kind}:{c['id']}", "kind": kind,
                            "author": (c.get("user") or {}).get("login", ""),
                            "association": c.get("author_association", "NONE"), "body": body,
                            "created_at": c.get("created_at") or c.get("submitted_at") or "",
                            "path": c.get("path"), "line": c.get("line") or c.get("original_line")})
        return sorted(out, key=lambda c: c["created_at"])

    def comment(self, number: int, body: str) -> None:
        self._with_file(body, lambda p: ["gh", "pr", "comment", str(number), "--body-file", p])

    def trusted(self, comment: dict[str, Any]) -> bool:
        allowed = self.settings.get("trusted_associations", list(TRUSTED_ASSOCIATIONS))
        return comment.get("association") in allowed
