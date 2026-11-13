"""Draft eval cases from jobs the factory already finished.

A done job has what history lacks: the ticket exactly as its requester wrote it (the work item,
not a commit message), the commit it started from, and a final commit the owner accepted and
merged (after any revisions). Those become the case's ticket, base and reference.

The factory wrote those references and their tests, so the drafts say which backend wrote each
one (`reference_by`) and keep the original job's numbers for comparison. A person reviews each
draft before it moves into cases/: keep behavioural hidden tests, and name in the acceptance
criteria any function or module the tests import.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import setup_wizard
from ..models import WorkItem
from ..projects import asf_home
from ..store import Store
from ..util import git, run, slugify
from .cases import Case, EvalError, Ticket


def state_dir_for(project: str) -> Path:
    """A project's state folder from its key (e.g. workflow-engine-11bf9a) or its repository path."""
    p = Path(project).expanduser()
    if (p / "factory.db").exists():
        return p
    if (asf_home() / project / "factory.db").exists():
        return asf_home() / project
    if p.is_dir():
        sd = setup_wizard.state_dir(p)
        if (sd / "factory.db").exists():
            return sd
    raise EvalError(f"no factory project {project!r}: give a project key under {asf_home()} or a repository path")


def backend_of(model: str) -> str:
    m = (model or "").lower()
    if m.startswith("claude") or "opus" in m or "sonnet" in m or "haiku" in m:
        return "claude"
    if any(k in m for k in ("gpt", "codex", "grok", "composer", "cursor")):
        return "cursor"
    return m or "unknown"


def import_jobs(project: str, job_ids: list[str] | None = None, states: tuple[str, ...] = ("done",)) -> list[Case]:
    sd = state_dir_for(project)
    repo = Path(json.loads((sd / "project.json").read_text())["repo"])
    store = Store(sd / "factory.db")
    out: list[Case] = []
    for job in store.jobs():
        if job_ids and job["id"] not in job_ids:
            continue
        if not job_ids and job["state"] not in states:
            continue
        d = job["data"]
        base = (d.get("sandbox") or {}).get("base_sha")
        ref = d.get("commit")
        if not base or not ref:
            continue
        if not run(["git", "-C", str(repo), "cat-file", "-e", f"{ref}^{{commit}}"], shell=False).ok:
            continue
        item: WorkItem = store.get_work_item(job["work_item_id"])
        impl = [r for r in store.runs(job["id"]) if r["role"] == "implementer"]
        by = backend_of(impl[-1]["model"]) if impl else "unknown"
        revisions = d.get("revisions") or []
        stat = git(repo, "diff", "--shortstat", base, ref).strip()
        events = list(store.events(job["id"]))
        minutes = (events[-1]["ts"] - events[0]["ts"]) / 60 if events else None
        name = repo.name
        out.append(Case(
            id=f"{slugify(name, 20)}-{job['id'].lower()}-{slugify(item.title, 36)}", repo=str(repo), base=base,
            reference=ref,
            ticket=Ticket(title=item.title, body=item.body, acceptance=list(item.acceptance),
                          kind=item.kind if item.kind in ("bug", "feature", "task", "chore") else "task"),
            tags=["imported", slugify(name, 30)] + (["revised"] if revisions else []),
            reference_by=by,
            notes=(f"Imported from {name} {job['id']} ({job['state']}). The ticket is the work item as written. "
                   "Review the hidden tests: keep behavioural ones, and name in the acceptance criteria any "
                   "function or module they import. Then `asf eval check` and move this file into cases/."),
            extra={"review": "needed", "imported": {
                "project": sd.name, "job": job["id"], "state": job["state"], "tier": job["tier"],
                "source": f"{item.source_kind}:{item.external_id}", "url": item.url,
                "implementer_backend": by, "cost_usd": round(store.job_cost(job["id"]), 2),
                "minutes": round(minutes, 1) if minutes else None,
                "fix_rounds": int(d.get("tester_rounds", 0)) + int(d.get("review_rounds", 0)) + int(d.get("ci_rounds", 0)),
                "revision_notes": [n for r in revisions for n in r.get("notes", [])],
                "diff": stat}}))
    return out

