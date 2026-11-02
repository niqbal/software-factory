"""Post-deploy gates. `none` passes; `sentry` fails on new issues after the merge."""

from __future__ import annotations

import time
import urllib.parse
from typing import Any

from ..models import GateResult
from .base import ObserveAdapter
from .intake import _http


class NoObserve(ObserveAdapter):
    kind = "none"

    def check(self, job: dict[str, Any], since_ts: float) -> GateResult:
        return GateResult("post_deploy", "skipped", "no observe provider configured")


class SentryObserve(ObserveAdapter):
    kind = "sentry"

    def check(self, job: dict[str, Any], since_ts: float) -> GateResult:
        token = self.ctx.secrets(self.settings.get("token_env", "SENTRY_AUTH_TOKEN"))
        if not token:
            return GateResult("post_deploy", "skipped", "SENTRY_AUTH_TOKEN not set")
        window = self.settings.get("window_minutes", 30) * 60
        wait = since_ts + window - time.time()
        if wait > 0 and not self.settings.get("no_wait"):
            time.sleep(min(wait, window))
        host = self.settings.get("host", "https://sentry.io")
        q = urllib.parse.urlencode({"query": "is:unresolved firstSeen:-%dm" % (window // 60)})
        url = f"{host}/api/0/projects/{self.settings['org']}/{self.settings['project']}/issues/?{q}"
        issues = _http.http_json("GET", url, {"Authorization": f"Bearer {token}"})
        allowed = self.settings.get("max_new_issues", 0)
        status = "pass" if len(issues) <= allowed else "fail"
        return GateResult("post_deploy", status, f"{len(issues)} new Sentry issues in {window // 60} min "
                                                 f"(allowed {allowed})")
