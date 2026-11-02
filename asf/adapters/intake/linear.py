"""Linear: issues moved to a trigger state (or labeled) become work items."""

from __future__ import annotations

import json
from typing import Any

from ...models import WorkItem
from ..base import IntakeAdapter
from . import _http

API = "https://api.linear.app/graphql"
PRIORITY_RISK = {1: "high", 2: "medium"}
QUERY = """query($team: String!, $state: String!) {
  issues(filter: {team: {key: {eq: $team}}, state: {name: {eq: $state}}}, first: 50) {
    nodes { id identifier title description url priority labels { nodes { name } } }
  }
}"""


class LinearIntake(IntakeAdapter):
    kind = "linear"

    def _to_item(self, d: dict[str, Any]) -> WorkItem:
        labels = d.get("labels") or []
        if isinstance(labels, dict):
            labels = labels.get("nodes", [])
        names = [lb["name"] for lb in labels]
        return WorkItem(source_kind="linear", external_id=d.get("identifier") or d["id"], title=d["title"],
                        body=d.get("description") or "", url=d.get("url", ""), labels=names,
                        kind="bug" if "Bug" in names or "bug" in names else "task",
                        priority=d.get("priority"), risk_hint=PRIORITY_RISK.get(d.get("priority") or 0))

    def poll(self) -> list[WorkItem]:
        key = self.ctx.secrets(self.settings.get("api_key_env", "LINEAR_API_KEY"))
        if not key:
            raise RuntimeError("LINEAR_API_KEY is not set")
        data = _http.http_json("POST", API, {"Authorization": key}, {
            "query": QUERY, "variables": {"team": self.settings["team"],
                                          "state": self.settings.get("trigger_state", "Ready for agent")}})
        return [self._to_item(n) for n in data["data"]["issues"]["nodes"]]

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        secret = self.ctx.secrets(self.settings.get("webhook_secret_env", "LINEAR_WEBHOOK_SECRET"))
        sig = _http.lower_headers(headers).get("linear-signature", "")
        return bool(secret) and _http.safe_eq(sig, _http.hmac_hex(secret, body))

    def normalize_webhook(self, headers: dict[str, str], body: bytes) -> list[WorkItem]:
        payload = json.loads(body)
        if payload.get("type") != "Issue" or payload.get("action") not in ("create", "update"):
            return []
        d = payload.get("data", {})
        state = (d.get("state") or {}).get("name")
        labels = [lb.get("name") for lb in d.get("labels", [])]
        trigger_label = self.settings.get("trigger_label")
        if state == self.settings.get("trigger_state", "Ready for agent") or \
                (trigger_label and trigger_label in labels):
            return [self._to_item(d)]
        return []
