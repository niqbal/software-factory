"""Slack incoming-webhook notifications. The URL comes from an env var, never from config."""

from __future__ import annotations

from typing import Any

from ..base import Notifier
from ..intake import _http

ICON = {"done": ":white_check_mark:", "failed": ":x:", "needs_human": ":raising_hand:",
        "paused": ":double_vertical_bar:", "awaiting_approval": ":eyes:"}


class SlackWebhookNotifier(Notifier):
    kind = "slack_webhook"

    def notify(self, event: str, job: dict[str, Any], message: str) -> None:
        url = self.ctx.secrets(self.settings.get("url_env", "SLACK_WEBHOOK_URL"))
        if not url:
            return
        text = f"{ICON.get(event, '')} *{job['id']}* {event.replace('_', ' ')}: {message}"
        _http.http_json("POST", url, body={"text": text[:3000]})
