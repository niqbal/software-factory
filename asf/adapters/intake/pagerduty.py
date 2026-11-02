"""PagerDuty v3 webhooks: triggered incidents become high-risk work items."""

from __future__ import annotations

import json

from ...models import WorkItem
from ..base import IntakeAdapter
from . import _http


class PagerDutyIntake(IntakeAdapter):
    kind = "pagerduty"

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        secret = self.ctx.secrets(self.settings.get("webhook_secret_env", "PAGERDUTY_WEBHOOK_SECRET"))
        sigs = _http.lower_headers(headers).get("x-pagerduty-signature", "").split(",")
        expected = "v1=" + _http.hmac_hex(secret, body) if secret else None
        return bool(expected) and any(_http.safe_eq(s.strip(), expected) for s in sigs)

    def normalize_webhook(self, headers: dict[str, str], body: bytes) -> list[WorkItem]:
        event = json.loads(body).get("event", {})
        if event.get("event_type") != "incident.triggered":
            return []
        d = event.get("data", {})
        service = (d.get("service") or {}).get("summary", "")
        return [WorkItem(source_kind="pagerduty", external_id=d.get("id", ""), title=d.get("title", "")[:120],
                         body=f"PagerDuty incident on {service}: {d.get('title')}\nUrgency: {d.get('urgency')}",
                         url=d.get("html_url", ""), kind="incident", risk_hint="high")]
