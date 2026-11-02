"""Slack: `/factory <request>` slash commands and @-mentions become work items."""

from __future__ import annotations

import json
import time
import urllib.parse

from ...models import WorkItem
from ..base import IntakeAdapter
from . import _http


class SlackIntake(IntakeAdapter):
    kind = "slack"

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        secret = self.ctx.secrets(self.settings.get("signing_secret_env", "SLACK_SIGNING_SECRET"))
        h = _http.lower_headers(headers)
        ts = h.get("x-slack-request-timestamp", "0")
        if not secret or abs(time.time() - int(ts or 0)) > 300:
            return False
        base = b"v0:" + ts.encode() + b":" + body
        return _http.safe_eq(h.get("x-slack-signature", ""), "v0=" + _http.hmac_hex(secret, base))

    def normalize_webhook(self, headers: dict[str, str], body: bytes) -> list[WorkItem]:
        ctype = _http.lower_headers(headers).get("content-type", "")
        if "application/x-www-form-urlencoded" in ctype:
            form = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode()).items()}
            text = form.get("text", "").strip()
            for verb in ("fix ", "build ", "do "):
                if text.lower().startswith(verb):
                    text = text[len(verb):]
            ext = f"{form.get('channel_id', '')}-{form.get('trigger_id', '')}"
            return [WorkItem(source_kind="slack", external_id=ext, title=text[:100], body=text,
                             reporter=form.get("user_name", ""),
                             url=form.get("response_url", ""))] if text else []
        payload = json.loads(body)
        event = payload.get("event", {})
        if payload.get("type") == "event_callback" and event.get("type") == "app_mention":
            text = " ".join(w for w in event.get("text", "").split() if not w.startswith("<@"))
            return [WorkItem(source_kind="slack", external_id=f"{event.get('channel')}-{event.get('ts')}",
                             title=text[:100], body=text, reporter=event.get("user", ""))] if text else []
        return []
