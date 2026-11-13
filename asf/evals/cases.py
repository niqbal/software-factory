"""Eval cases and where they live.

A case is one YAML file:

    id: invoice-discount-rounding          # unique within the set; defaults to the file name
    repo: /Users/me/work/invoice           # local path or clone URL
    base: 1a2b3c...                        # the commit the requester saw
    reference: 4d5e6f...                   # the merged solution (its tests are the hidden tests)
    ticket:                                # exactly as the requester wrote it
      title: Discount truncates instead of rounding
      body: |
        apply_discount(999, 50) returns 499; it should be 500.
      acceptance: []                       # only if the requester wrote some
      kind: bug                            # bug | feature | task | chore
    # Optional:
    hidden_tests: [tests/test_discount.py] # default: the test files the reference adds or changes
    hidden_dir: invoice-discount/hidden    # authored hidden tests: files written over the candidate
                                           # at the same relative paths (instead of the reference's)
    reference_patch: invoice-discount/ref.diff  # a reference written for the eval, applied on base
                                           # (instead of `reference`; kept in the archive only)
    tags: [authored, edge-case]            # the report splits results by tag
    reference_by: cursor                   # who wrote the reference: human, claude, cursor, ...
    needs_app: true                        # hidden tests are browser tests: start app.start for them
    test_command: "pytest -q {tests}"      # default: the owning package's test command
    setup: []                              # commands run in each fresh checkout
    deps_from: /Users/me/work/invoice      # copy execution.clone_dirs (e.g. .venv) from here
    config: {...} | path/to/factory.yaml   # base factory.yaml (default: see workspace.base_config)
    notes: why this case is in the set

The archive (default ~/work/asf-runs/evals, or $ASF_EVALS, or --evals):

    variants/<name>.yaml                   your own variants (override the built-in ones)
    sets/<set>/cases/<id>.yaml             the cases
    sets/<set>/drafts/<id>.yaml            `asf eval mine` output, for a human to prune
    sets/<set>/checks/<id>.json            `asf eval check`: hidden tests on base and reference
    sets/<set>/runs/<run>/<variant>/<case>-r<k>/result.json (+ candidate.diff, hidden.log, events.jsonl)
    sets/<set>/reports/<stamp>.{json,md}, latest.{json,md}
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..util import run, slugify

DEFAULT_EVALS = "~/work/asf-runs/evals"
KINDS = ("bug", "feature", "task", "chore")


class EvalError(ValueError):
    pass


@dataclass
class Ticket:
    title: str
    body: str = ""
    acceptance: list[str] = field(default_factory=list)
    kind: str = "task"

    def text(self) -> str:
        """The ticket as a person would paste it: title, body, then acceptance criteria if any."""
        out = self.title.strip()
        if self.body.strip():
            out += "\n\n" + self.body.strip()
        if self.acceptance:
            out += "\n\nAcceptance criteria:\n" + "\n".join(f"- {a}" for a in self.acceptance)
        return out


@dataclass
class Case:
    id: str
    repo: str
    base: str
    reference: str
    ticket: Ticket
    hidden_tests: list[str] = field(default_factory=list)
    test_command: str = ""
    setup: list[str] = field(default_factory=list)
    deps_from: str = ""
    config: Any = None
    notes: str = ""
    hidden_dir: str = ""
    reference_patch: str = ""
    tags: list[str] = field(default_factory=list)
    reference_by: str = ""
    needs_app: bool = False
    path: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def local_repo(self) -> Path | None:
        """The repository as a local folder, when `repo` is a path rather than a URL."""
        if re.match(r"^[\w+.-]+://", self.repo) or re.match(r"^[\w.-]+@[\w.-]+:", self.repo):
            return None
        p = Path(self.repo).expanduser()
        if not p.is_absolute() and self.path:
            p = (self.path.parent / p)
        return p.resolve()

    def _rel(self, value: str) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() or not self.path else (self.path.parent / p)

    @property
    def hidden_folder(self) -> Path | None:
        return self._rel(self.hidden_dir) if self.hidden_dir else None

    @property
    def reference_patch_file(self) -> Path | None:
        return self._rel(self.reference_patch) if self.reference_patch else None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "repo": self.repo, "base": self.base,
                             **({"reference": self.reference} if self.reference else {}),
                             "ticket": {"title": self.ticket.title, "body": self.ticket.body,
                                        "acceptance": list(self.ticket.acceptance), "kind": self.ticket.kind}}
        for key in ("hidden_tests", "hidden_dir", "reference_patch", "test_command", "setup", "deps_from",
                    "config", "tags", "reference_by", "needs_app", "notes"):
            value = getattr(self, key)
            if value:
                d[key] = value
        d.update(self.extra)
        return d


def parse_case(data: dict[str, Any], path: Path | None = None) -> Case:
    if not isinstance(data, dict):
        raise EvalError(f"{path}: a case must be a mapping")
    missing = [k for k in ("repo", "base", "ticket") if not data.get(k)]
    if not data.get("reference") and not data.get("reference_patch"):
        missing.append("reference (or reference_patch)")
    if missing:
        raise EvalError(f"{path or 'case'}: missing {', '.join(missing)}")
    t = data["ticket"]
    if isinstance(t, str):
        t = {"title": t}
    if not isinstance(t, dict) or not str(t.get("title") or "").strip():
        raise EvalError(f"{path or 'case'}: ticket needs a title")
    kind = str(t.get("kind") or "task")
    if kind not in KINDS:
        raise EvalError(f"{path or 'case'}: ticket.kind must be one of {', '.join(KINDS)}")
    known = {"id", "repo", "base", "reference", "ticket", "hidden_tests", "test_command", "setup",
             "deps_from", "config", "notes", "hidden_dir", "reference_patch", "tags", "reference_by", "needs_app"}
    case_id = str(data.get("id") or (path.stem if path else ""))
    if not case_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", case_id):
        raise EvalError(f"{path or 'case'}: id must be letters, digits, '.', '_' or '-' (got {case_id!r})")
    return Case(id=case_id, repo=str(data["repo"]), base=str(data["base"]), reference=str(data.get("reference") or ""),
                ticket=Ticket(title=str(t["title"]), body=str(t.get("body") or ""),
                              acceptance=[str(a) for a in t.get("acceptance") or []], kind=kind),
                hidden_tests=[str(h) for h in data.get("hidden_tests") or []],
                test_command=str(data.get("test_command") or ""),
                setup=[str(s) for s in data.get("setup") or []],
                deps_from=str(data.get("deps_from") or ""), config=data.get("config"),
                notes=str(data.get("notes") or ""), hidden_dir=str(data.get("hidden_dir") or ""),
                reference_patch=str(data.get("reference_patch") or ""),
                tags=[str(t) for t in data.get("tags") or []], reference_by=str(data.get("reference_by") or ""),
                needs_app=bool(data.get("needs_app")),
                path=path,
                extra={k: v for k, v in data.items() if k not in known})


def load_case(path: Path) -> Case:
    return parse_case(yaml.safe_load(Path(path).read_text()), Path(path))


def dump_case(case: Case) -> str:
    return yaml.safe_dump(case.to_dict(), sort_keys=False, allow_unicode=True, width=100)


# ----------------------------------------------------------------------------- the archive

def evals_root(explicit: str | Path | None = None) -> Path:
    return Path(explicit or os.environ.get("ASF_EVALS") or DEFAULT_EVALS).expanduser().resolve()


def _toplevel(path: Path) -> Path | None:
    p = path
    while not p.exists():
        p = p.parent
    res = run(["git", "-C", str(p), "rev-parse", "--show-toplevel"], shell=False)
    return Path(res.stdout.strip()).resolve() if res.ok and res.stdout.strip() else None


def guard_archive(root: Path, repos: list[Path] | None = None) -> None:
    """Refuse an archive inside this code repository or inside a product repository.

    Cases hold hidden tests and results hold product code: they belong in the private runs
    archive only. The archive may itself be a git repository (asf-runs is)."""
    root = root.resolve()
    code = Path(__file__).resolve().parents[2]
    top = _toplevel(root)
    for repo in [code, *(repos or [])]:
        repo = repo.resolve()
        if root == repo or repo in root.parents or (top is not None and top == repo):
            raise EvalError(f"refusing to keep eval data in {root}: it is inside the repository {repo}. "
                            "Eval cases and results live only in the private runs archive "
                            f"(default {DEFAULT_EVALS}, or set ASF_EVALS / --evals).")


@dataclass
class EvalSet:
    root: Path      # the archive
    name: str

    @property
    def dir(self) -> Path:
        return self.root / "sets" / self.name

    @property
    def cases_dir(self) -> Path:
        return self.dir / "cases"

    @property
    def runs_dir(self) -> Path:
        return self.dir / "runs"

    @property
    def reports_dir(self) -> Path:
        return self.dir / "reports"

    def cases(self, only: list[str] | None = None) -> list[Case]:
        if not self.cases_dir.is_dir():
            raise EvalError(f"no cases in {self.cases_dir}: add case YAML files there "
                            f"(`asf eval mine` drafts some into {self.dir / 'drafts'})")
        out = [load_case(p) for p in sorted(self.cases_dir.glob("*.y*ml"))]
        seen: set[str] = set()
        for c in out:
            if c.id in seen:
                raise EvalError(f"duplicate case id {c.id} in {self.cases_dir}")
            seen.add(c.id)
        if only:
            wanted = set(only)
            unknown = wanted - seen
            if unknown:
                raise EvalError(f"unknown case(s) in set {self.name}: {', '.join(sorted(unknown))}")
            out = [c for c in out if c.id in wanted]
        if not out:
            raise EvalError(f"set {self.name} has no cases in {self.cases_dir}")
        return out


def case_slug(repo_name: str, sha: str, title: str) -> str:
    return f"{slugify(repo_name, 24)}-{sha[:8]}-{slugify(title, 40)}".strip("-")
