"""Markdown files with YAML front matter in an inbox folder.

    ---
    title: Fix rounding in invoice totals
    kind: bug
    labels: [tier:1]
    acceptance:
      - totals round half-up to cents
    ---
    Free-text description...
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ...models import WorkItem
from ..base import IntakeAdapter


def parse(path: Path) -> WorkItem:
    text = path.read_text()
    meta, body = {}, text
    if text.startswith("---"):
        _, front, body = text.split("---", 2)
        meta = yaml.safe_load(front) or {}
    return WorkItem(source_kind="local", external_id=path.stem, title=meta.get("title", path.stem),
                    body=body.strip(), acceptance=meta.get("acceptance", []) or [],
                    labels=meta.get("labels", []) or [], kind=meta.get("kind", "task"),
                    risk_hint=meta.get("risk"), url=str(path))


class LocalFileIntake(IntakeAdapter):
    kind = "local"

    @property
    def inbox(self) -> Path:
        p = Path(self.settings.get("path", self.ctx.state_dir / "inbox"))
        return p if p.is_absolute() else self.ctx.repo / p

    def poll(self) -> list[WorkItem]:
        if not self.inbox.exists():
            return []
        return [parse(p) for p in sorted(self.inbox.glob("*.md"))]

    def fetch(self, external_id: str) -> WorkItem:
        return parse(self.inbox / f"{external_id}.md")
