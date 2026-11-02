"""Beads (bd) issue tracker, the git-native tracker many agent-driven repos use.

Polls `bd ready`, which already excludes blocked issues. Filters by type,
priority and labels from settings. Write-back (a comment on the issue) is off
unless settings.write_back is true.
"""

from __future__ import annotations

import json
from typing import Any

from ...models import WorkItem
from ...util import run
from ..base import IntakeAdapter

KIND = {"bug": "bug", "feature": "feature", "task": "task", "chore": "chore", "epic": "epic"}


class BeadsIntake(IntakeAdapter):
    kind = "beads"

    def _bd(self, *args: str) -> Any:
        runner = self.ctx.runner or run
        res = runner(["bd", *args, "--json"], cwd=self.ctx.repo, shell=False, timeout=60)
        if not res.ok:
            raise RuntimeError(f"bd {' '.join(args)} failed: {res.tail(800)}")
        return json.loads(res.stdout or "[]")

    def _to_item(self, d: dict[str, Any]) -> WorkItem:
        labels = d.get("labels") or []
        return WorkItem(source_kind="beads", external_id=d["id"], title=d["title"],
                        body=(d.get("description") or "") + (("\n\nDesign notes:\n" + d["design"])
                                                             if d.get("design") else ""),
                        acceptance=[a for a in (d.get("acceptance_criteria") or "").splitlines() if a.strip()],
                        labels=labels, kind=KIND.get(d.get("issue_type", "task"), "task"),
                        priority=d.get("priority"), reporter=d.get("created_by", ""))

    def _wanted(self, d: dict[str, Any]) -> bool:
        s = self.settings
        if s.get("ids") and d["id"] not in s["ids"]:
            return False
        if d.get("issue_type") in s.get("exclude_types", ["epic"]):
            return False
        if s.get("types") and d.get("issue_type") not in s["types"]:
            return False
        if "max_priority" in s and (d.get("priority") is None or d["priority"] > s["max_priority"]):
            return False
        if s.get("labels") and not set(s["labels"]) & set(d.get("labels") or []):
            return False
        return True

    def poll(self) -> list[WorkItem]:
        return [self._to_item(d) for d in self._bd("ready") if self._wanted(d)]

    def fetch(self, external_id: str) -> WorkItem:
        data = self._bd("show", external_id)
        return self._to_item(data[0] if isinstance(data, list) else data)

    def report(self, item: WorkItem, status: str, message: str) -> None:
        if self.settings.get("write_back"):
            (self.ctx.runner or run)(["bd", "comments", "add", item.external_id,
                                      f"[factory] {status}: {message}"], cwd=self.ctx.repo, shell=False)
