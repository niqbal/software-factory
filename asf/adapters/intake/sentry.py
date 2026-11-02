"""Sentry: new or regressed issues above a threshold become bug work items."""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from ...models import WorkItem
from ..base import IntakeAdapter
from . import _http


class SentryIntake(IntakeAdapter):
    kind = "sentry"

    def _to_item(self, issue: dict[str, Any]) -> WorkItem:
        meta = issue.get("metadata") or {}
        body = "\n".join(filter(None, [
            f"Sentry issue {issue.get('shortId') or issue.get('id')}: {issue.get('title')}",
            f"Culprit: {issue.get('culprit')}" if issue.get("culprit") else "",
            f"Error: {meta.get('type', '')}: {meta.get('value', '')}" if meta else "",
            f"Events: {issue.get('count')}, users: {issue.get('userCount')}",
            f"First seen {issue.get('firstSeen')}, last seen {issue.get('lastSeen')}",
        ]))
        return WorkItem(source_kind="sentry", external_id=str(issue["id"]), title=issue.get("title", "")[:120],
                        body=body, url=issue.get("permalink", ""), kind="bug",
                        risk_hint=self.settings.get("risk", "medium"),
                        acceptance=["The error no longer occurs for the reported input",
                                    "A regression test reproduces the original failure"])

    def poll(self) -> list[WorkItem]:
        token = self.ctx.secrets(self.settings.get("token_env", "SENTRY_AUTH_TOKEN"))
        if not token:
            raise RuntimeError("SENTRY_AUTH_TOKEN is not set")
        host = self.settings.get("host", "https://sentry.io")
        q = urllib.parse.urlencode({"query": "is:unresolved", "statsPeriod": "24h"})
        url = f"{host}/api/0/projects/{self.settings['org']}/{self.settings['project']}/issues/?{q}"
        issues = _http.http_json("GET", url, {"Authorization": f"Bearer {token}"})
        floor = self.settings.get("min_events", 20)
        return [self._to_item(i) for i in issues if int(i.get("count", 0)) >= floor]

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        secret = self.ctx.secrets(self.settings.get("client_secret_env", "SENTRY_CLIENT_SECRET"))
        sig = _http.lower_headers(headers).get("sentry-hook-signature", "")
        return bool(secret) and _http.safe_eq(sig, _http.hmac_hex(secret, body))

    def normalize_webhook(self, headers: dict[str, str], body: bytes) -> list[WorkItem]:
        resource = _http.lower_headers(headers).get("sentry-hook-resource", "")
        payload = json.loads(body)
        if resource == "issue" and payload.get("action") in ("created", "unresolved"):
            return [self._to_item(payload["data"]["issue"])]
        if resource == "event_alert":
            event = payload.get("data", {}).get("event", {})
            return [self._to_item({"id": event.get("issue_id"), "title": event.get("title", ""),
                                   "culprit": event.get("culprit"), "permalink": event.get("web_url", ""),
                                   "metadata": event.get("metadata")})]
        return []
