"""Every project this factory works on.

A project is a state directory under $ASF_HOME (default ~/.asf) holding factory.db. Its repo path
is recorded in project.json the first time any asf command touches it. For older state dirs the
path is recovered by looking for a directory with the project's name whose path hashes to the
state dir's suffix. ~/.asf/projects.yaml lets the owner name, order, prioritise, hide or archive
projects; it is optional and never required.

    projects:
      workflow-engine-11bf9a: {name: Workflow engine, priority: 1}
      CycleSense-98c570: {archived: true}
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .util import sha

SEARCH_ROOTS = ["~/work", "~/code", "~/src", "~/projects", "~"]


def asf_home() -> Path:
    return Path(os.environ.get("ASF_HOME", "~/.asf")).expanduser()


@dataclass
class Project:
    key: str                 # state dir name, e.g. workflow-engine-11bf9a
    state_dir: Path
    repo: Path | None
    name: str
    priority: int = 100      # lower runs first when projects share one model quota
    hidden: bool = False
    archived: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.repo is not None and self.repo.exists()


def record(state_dir: Path, repo: Path) -> None:
    """Remember which repo a state dir belongs to (called whenever asf opens a project)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    p = state_dir / "project.json"
    data = {"repo": str(repo.resolve())}
    try:
        if p.exists() and json.loads(p.read_text()) == data:
            return
    except Exception:  # noqa: BLE001
        pass
    p.write_text(json.dumps(data) + "\n")


def _recover_repo(state_dir: Path) -> Path | None:
    """Find the repo whose path hashes to the state dir's suffix (for dirs made before project.json)."""
    name, _, digest = state_dir.name.rpartition("-")
    if not name or not digest:
        return None
    for root in SEARCH_ROOTS:
        base = Path(root).expanduser()
        if not base.is_dir():
            continue
        depth = 0 if base == Path.home() else 3
        frontier = [base]
        for _ in range(depth + 1):
            nxt = []
            for d in frontier:
                cand = d / name
                if cand.is_dir() and sha(str(cand.resolve()), len(digest)) == digest:
                    return cand.resolve()
                try:
                    nxt += [c for c in d.iterdir() if c.is_dir() and not c.name.startswith(".")
                            and c.name not in ("node_modules", "Library", ".venv")]
                except OSError:
                    continue
            frontier = nxt[:2000]
    return None


def load_registry(home: Path | None = None) -> dict[str, dict[str, Any]]:
    p = (home or asf_home()) / "projects.yaml"
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text()) or {}
    return data.get("projects", {}) or {}


def discover(home: Path | None = None, include_archived: bool = True) -> list[Project]:
    home = home or asf_home()
    reg = load_registry(home)
    out = []
    for state_dir in sorted(home.iterdir()) if home.exists() else []:
        if not (state_dir / "factory.db").exists():
            continue
        entry = reg.get(state_dir.name, {}) or {}
        repo = Path(entry["repo"]).expanduser() if entry.get("repo") else None
        if repo is None and (state_dir / "project.json").exists():
            try:
                repo = Path(json.loads((state_dir / "project.json").read_text())["repo"])
            except Exception:  # noqa: BLE001
                repo = None
        if repo is None:
            repo = _recover_repo(state_dir)
            if repo:
                record(state_dir, repo)
        p = Project(key=state_dir.name, state_dir=state_dir, repo=repo,
                    name=entry.get("name") or (repo.name if repo else state_dir.name.rpartition("-")[0]),
                    priority=int(entry.get("priority", 100)), hidden=bool(entry.get("hidden")),
                    archived=bool(entry.get("archived")), extra=entry)
        if p.archived and not include_archived:
            continue
        out.append(p)
    out.sort(key=lambda p: (p.archived, p.priority, p.name.lower()))
    return out


def set_entry(key: str, **fields: Any) -> None:
    """Update one project's registry entry (used by the console's archive/priority controls)."""
    home = asf_home()
    p = home / "projects.yaml"
    data = (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}
    projects = data.setdefault("projects", {})
    entry = projects.setdefault(key, {})
    for k, v in fields.items():
        if v is None:
            entry.pop(k, None)
        else:
            entry[k] = v
    p.write_text(yaml.safe_dump(data, sort_keys=False))
