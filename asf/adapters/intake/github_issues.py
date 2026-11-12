"""GitHub Issues: poll with `gh`, or receive `issues` webhooks.

Trigger: an issue carrying the configured label (default "factory").
"""

from __future__ import annotations

import json
import os
from typing import Any

from ...models import WorkItem
from ...util import run
from ..base import IntakeAdapter
from ._http import hmac_hex, lower_headers, safe_eq

FIELDS = "number,title,body,labels,url,author"
TRUSTED = ("OWNER", "MEMBER", "COLLABORATOR")


def _epoch(iso: str | None) -> float | None:
    from datetime import datetime
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _kind(labels: list[str]) -> str:
    for k in ("bug", "feature", "chore"):
        if k in labels:
            return k
    return "task"


_TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN")


def _env_token() -> bool:
    return any(os.environ.get(v) for v in _TOKEN_VARS)


def _without_env_token() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _TOKEN_VARS}


class GitHubIssuesIntake(IntakeAdapter):
    kind = "github_issues"

    @property
    def label(self) -> str:
        return self.settings.get("label", "factory")

    def _gh(self, *args: str) -> Any:
        res = (self.ctx.runner or run)(["gh", *args], cwd=self.ctx.repo, shell=False, timeout=60)
        if not res.ok and "401" in (res.stdout + res.stderr) and _env_token():
            # An inherited GITHUB_TOKEN/GH_TOKEN overrides gh's own sign-in; when GitHub rejects
            # it, retry with the sign-in instead.
            res = (self.ctx.runner or run)(["gh", *args], cwd=self.ctx.repo, shell=False, timeout=60,
                                           env=_without_env_token())
        if not res.ok:
            raise RuntimeError(f"gh {' '.join(args[:3])} failed: {res.tail(800)}")
        return json.loads(res.stdout or "null")

    def _to_item(self, d: dict[str, Any]) -> WorkItem:
        labels = [lb["name"] if isinstance(lb, dict) else lb for lb in d.get("labels", [])]
        author = d.get("author") or d.get("user") or {}
        return WorkItem(source_kind="github_issues", external_id=str(d["number"]), title=d["title"],
                        body=d.get("body") or "", url=d.get("url") or d.get("html_url", ""),
                        labels=labels, kind=_kind(labels), reporter=author.get("login", ""))

    def poll(self) -> list[WorkItem]:
        rows = self._gh("issue", "list", "--label", self.label, "--state", "open", "--json", FIELDS,
                        "--limit", str(self.settings.get("limit", 20)))
        return [self._to_item(r) for r in rows]

    def fetch(self, external_id: str) -> WorkItem:
        return self._to_item(self._gh("issue", "view", external_id, "--json", FIELDS))

    def comment(self, external_id: str, body: str) -> None:
        """Post a comment on the issue (the factory asking, or relaying a console answer)."""
        res = (self.ctx.runner or run)(["gh", "issue", "comment", external_id, "--body", body],
                                       cwd=self.ctx.repo, shell=False, timeout=60)
        if not res.ok and "401" in (res.stdout + res.stderr) and _env_token():
            res = (self.ctx.runner or run)(["gh", "issue", "comment", external_id, "--body", body],
                                           cwd=self.ctx.repo, shell=False, timeout=60, env=_without_env_token())
        if not res.ok:
            raise RuntimeError(f"gh issue comment {external_id} failed: {res.tail(800)}")

    def replies(self, external_id: str, since: float) -> list[dict[str, Any]]:
        """Comments posted after `since` by people with write access to the repo (owner,
        member, collaborator), oldest first. Comments the factory posted itself are skipped.
        Anyone else's comment is ignored: it could steer the agents."""
        data = self._gh("issue", "view", external_id, "--json", "comments") or {}
        out = []
        for c in data.get("comments") or []:
            body = c.get("body") or ""
            if "<!-- asf:" in body or c.get("authorAssociation") not in TRUSTED:
                continue
            at = _epoch(c.get("createdAt"))
            if at is None or at <= since:
                continue
            out.append({"id": c.get("id") or c.get("url"), "author": (c.get("author") or {}).get("login", ""),
                        "body": body, "at": at})
        return sorted(out, key=lambda c: c["at"])

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        secret = self.ctx.secrets(self.settings.get("webhook_secret_env", "GITHUB_WEBHOOK_SECRET"))
        sig = lower_headers(headers).get("x-hub-signature-256", "")
        return bool(secret) and safe_eq(sig, "sha256=" + hmac_hex(secret, body))

    def normalize_webhook(self, headers: dict[str, str], body: bytes) -> list[WorkItem]:
        if lower_headers(headers).get("x-github-event") != "issues":
            return []
        payload = json.loads(body)
        issue = payload.get("issue", {})
        labelled = payload.get("action") == "labeled" and payload.get("label", {}).get("name") == self.label
        opened = payload.get("action") == "opened" and any(
            lb.get("name") == self.label for lb in issue.get("labels", []))
        return [self._to_item(issue)] if (labelled or opened) else []

    def report(self, item: WorkItem, status: str, message: str) -> None:
        if self.settings.get("write_back"):
            (self.ctx.runner or run)(["gh", "issue", "comment", item.external_id, "--body",
                                      f"**Software factory: {status}**\n\n{message}"],
                                     cwd=self.ctx.repo, shell=False)
