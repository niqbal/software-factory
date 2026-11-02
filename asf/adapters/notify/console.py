from __future__ import annotations

from typing import Any

from ..base import Notifier


class ConsoleNotifier(Notifier):
    kind = "console"

    def notify(self, event: str, job: dict[str, Any], message: str) -> None:
        print(f"[factory] {job['id']} {event}: {message}", flush=True)
