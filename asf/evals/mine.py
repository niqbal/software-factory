"""Draft eval cases from a repository's merged history.

Walks the default branch's first-parent history (so a merged pull request is one unit: its first
parent is the base, the merge is the reference) and keeps changes that:

- touch both code and tests (the tests become the hidden tests),
- are small (under `max_lines` changed lines, tests included),
- have a ticket to start from: a referenced issue (#123, fixes/closes ...), or a commit message
  descriptive enough to stand in for one.

The ticket text is drafted from the issue (with `fetch_issues`, through the GitHub CLI) or from
the commit message, and marked `review: needed`. A commit message describes the solution, not
the request, so a person rewrites each kept draft as its requester would have written it, then
moves it into the set's cases/ folder. Drafts are never trusted as they are.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from ..config import DEFAULT_TEST_GLOBS
from ..util import any_match, git, run
from .cases import Case, Ticket, case_slug
from .score import RUNNABLE

ISSUE_REF = re.compile(r"(?:(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s+)?(?:[\w.-]+/[\w.-]+)?#(\d+)", re.I)
PR_MERGE = re.compile(r"^Merge pull request #(\d+)", re.I)
TRIVIAL = re.compile(r"^(wip|fix|fixes|update|updates|cleanup|lint|format|typo|bump|merge)\b[\s.:!]*$", re.I)
# Git trailers (Co-Authored-By, Signed-off-by, Factory-Job, Replit-Commit-...): not part of a ticket.
TRAILER = re.compile(r"^[A-Za-z][\w-]*: \S")
CODE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".rb", ".php",
            ".cs", ".swift", ".scala", ".c", ".cc", ".cpp", ".h", ".vue", ".svelte"}


def strip_trailers(body: str) -> str:
    """The message body without its closing paragraph of git trailers."""
    paras = re.split(r"\n\s*\n", body.strip())
    if paras and all(TRAILER.match(ln) for ln in paras[-1].splitlines() if ln.strip()):
        paras.pop()
    return "\n\n".join(paras).strip()


def _kind(text: str) -> str:
    t = text.lower()
    if re.search(r"\b(fix|bug|crash|error|broken|regression|wrong|incorrect)", t):
        return "bug"
    if re.search(r"\b(add|support|feature|new|allow|introduce|implement)", t):
        return "feature"
    return "task"


def _first_parent_commits(repo: Path, branch: str, since: str | None, scan: int) -> list[dict[str, Any]]:
    args = ["log", "--first-parent", f"--max-count={scan}", "--format=%H%x1f%P%x1f%ad%x1f%B%x1e", "--date=short"]
    if since:
        args.append(f"--since={since}")
    out = git(repo, *args, branch)
    rows = []
    for rec in out.split("\x1e"):
        rec = rec.strip("\n")
        if not rec.strip():
            continue
        h, parents, date, msg = (rec.split("\x1f") + ["", "", ""])[:4]
        parents = parents.split()
        if not parents:
            continue  # the root commit has no base
        rows.append({"sha": h.strip(), "base": parents[0], "merge": len(parents) > 1, "date": date,
                     "message": msg.strip()})
    return rows


def _stats(repo: Path, base: str, ref: str, test_globs: list[str]) -> dict[str, Any]:
    code, tests, lines = [], [], 0
    for line in git(repo, "diff", "--numstat", "--no-renames", base, ref).splitlines():
        added, deleted, path = (line.split("\t", 2) + ["", ""])[:3]
        lines += (int(added) if added.isdigit() else 0) + (int(deleted) if deleted.isdigit() else 0)
        if any_match(path, test_globs):
            tests.append(path)
        elif Path(path).suffix in CODE_EXT:
            code.append(path)
    return {"code": code, "tests": tests, "lines": lines}


def _issue(repo: Path, number: str) -> dict[str, Any] | None:
    gh = shutil.which("gh")
    if not gh:
        return None
    res = run([gh, "issue", "view", number, "--json", "title,body,labels"], cwd=repo, shell=False, timeout=30)
    if not res.ok:
        return None
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return None


def mine(repo: Path, since: str | None = None, limit: int = 30, max_lines: int = 400, branch: str = "",
         fetch_issues: bool = False, test_globs: list[str] | None = None, scan: int = 2000) -> list[Case]:
    """Draft cases, newest first. See the module docstring for what is kept."""
    repo = repo.expanduser().resolve()
    branch = branch or git(repo, "symbolic-ref", "--short", "HEAD", check=False).strip() or "HEAD"
    globs = test_globs or DEFAULT_TEST_GLOBS
    out: list[Case] = []
    for c in _first_parent_commits(repo, branch, since, scan):
        if len(out) >= limit:
            break
        msg = c["message"]
        subject = msg.splitlines()[0].strip() if msg else ""
        if "Factory-Job:" in msg:
            continue  # the factory's own work: its ticket is in the factory's records, not here
        st = _stats(repo, c["base"], c["sha"], globs)
        if not st["code"] or not st["lines"] or st["lines"] > max_lines:
            continue
        if not any(any_match(t, RUNNABLE) for t in st["tests"]):
            continue  # helpers or fixtures only: nothing a test runner can be pointed at
        refs = [m.group(1) for m in ISSUE_REF.finditer(msg)]
        pr = PR_MERGE.match(subject)
        if pr:
            refs = [r for r in refs if r != pr.group(1)]
            # GitHub's merge message: the PR title is the first line of the body.
            body_lines = [ln for ln in msg.splitlines()[1:] if ln.strip()]
            subject = body_lines[0].strip() if body_lines else subject
        descriptive = len(subject.split()) >= 4 and not TRIVIAL.match(subject)
        if not refs and not descriptive:
            continue
        issue = _issue(repo, refs[0]) if (fetch_issues and refs) else None
        if issue:
            ticket = Ticket(title=issue.get("title") or subject, body=issue.get("body") or "",
                            kind=_kind((issue.get("title") or "") + " " + " ".join(
                                lb.get("name", "") for lb in issue.get("labels") or [])))
            source = f"issue #{refs[0]}"
        else:
            body = "\n".join(msg.splitlines()[1:]).strip()
            body = strip_trailers(body)
            ticket = Ticket(title=subject, body=body, kind=_kind(subject))
            source = "commit message"
        out.append(Case(
            id=case_slug(repo.name, c["sha"], subject), repo=str(repo), base=c["base"], reference=c["sha"],
            ticket=ticket, hidden_tests=[],
            notes=(f"Drafted by `asf eval mine` from {source}. Rewrite the ticket as the requester would have "
                   "written it (the problem, not the solution), check the hidden tests fail on base and pass "
                   "on the reference (`asf eval check`), then move this file into cases/."),
            extra={"review": "needed", "mined": {
                "date": c["date"], "merge": c["merge"], "issues": refs, "ticket_from": source,
                "changed_lines": st["lines"], "code_files": st["code"], "test_files": st["tests"]}}))
    return out
