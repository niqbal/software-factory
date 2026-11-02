"""Jira Cloud: poll with a JQL filter, or receive issue webhooks."""

from __future__ import annotations

import base64
import json
import urllib.parse
from typing import Any

from ...models import WorkItem
from ..base import IntakeAdapter
from . import _http

RISK = {"Highest": "high", "High": "high", "Medium": "medium"}


def adf_text(node: Any) -> str:
    """Flatten Atlassian Document Format to plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if node.get("type") == "text":
        return node.get("text", "")
    parts = [adf_text(c) for c in node.get("content", [])]
    sep = "\n" if node.get("type") in ("doc", "bulletList", "orderedList") else ""
    text = sep.join(parts)
    return text + ("\n" if node.get("type") in ("paragraph", "listItem", "heading") else "")


class JiraIntake(IntakeAdapter):
    kind = "jira"

    def _auth(self) -> dict[str, str]:
        email = self.ctx.secrets(self.settings.get("email_env", "JIRA_EMAIL"))
        token = self.ctx.secrets(self.settings.get("token_env", "JIRA_API_TOKEN"))
        if not (email and token):
            raise RuntimeError("JIRA_EMAIL and JIRA_API_TOKEN must be set")
        return {"Authorization": "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()}

    def _to_item(self, issue: dict[str, Any]) -> WorkItem:
        f = issue.get("fields", {})
        itype = (f.get("issuetype") or {}).get("name", "Task")
        site = self.settings.get("site", "")
        return WorkItem(source_kind="jira", external_id=issue["key"], title=f.get("summary", ""),
                        body=adf_text(f.get("description")).strip(), labels=f.get("labels", []),
                        url=f"{site}/browse/{issue['key']}" if site else "",
                        kind="bug" if itype == "Bug" else "task",
                        risk_hint=RISK.get((f.get("priority") or {}).get("name", "")))

    def poll(self) -> list[WorkItem]:
        jql = self.settings.get("jql", 'labels = factory AND statusCategory != Done')
        url = f"{self.settings['site']}/rest/api/3/search/jql?" + urllib.parse.urlencode(
            {"jql": jql, "fields": "summary,description,labels,priority,issuetype", "maxResults": 50})
        data = _http.http_json("GET", url, self._auth())
        return [self._to_item(i) for i in data.get("issues", [])]

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        secret = self.ctx.secrets(self.settings.get("webhook_secret_env", "JIRA_WEBHOOK_SECRET"))
        sig = _http.lower_headers(headers).get("x-hub-signature", "")
        return bool(secret) and _http.safe_eq(sig, "sha256=" + _http.hmac_hex(secret, body))

    def normalize_webhook(self, headers: dict[str, str], body: bytes) -> list[WorkItem]:
        payload = json.loads(body)
        issue = payload.get("issue")
        label = self.settings.get("label", "factory")
        if not issue or label not in issue.get("fields", {}).get("labels", []):
            return []
        return [self._to_item(issue)]
