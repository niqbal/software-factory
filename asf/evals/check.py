"""Check a case before it counts: what its hidden tests say on the base commit and on the reference.

The hidden tests run once on base and twice on the reference, and every named test is sorted:

- fail-to-pass: fails (or is missing) on base, passes on the reference both times. What the
  ticket asks for: a candidate resolves the case only if every one of these passes.
- pass-to-pass: passes on base and on the reference. Must not break.
- flaky: a different result on the two reference runs. Left out of scoring.
- fails on the reference: broken or environment-bound. Left out of scoring.

A case with no fail-to-pass test does not test the change and is refused. Results go to
sets/<set>/checks/<id>.json with a hash of the case file, so an edited case is checked again.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from ..util import git, sha
from . import score, workspace
from .cases import Case, EvalSet


def case_hash(case: Case) -> str:
    parts = [case.path.read_text() if case.path and case.path.exists() else json.dumps(case.to_dict())]
    for folder in (case.hidden_folder,):
        if folder and folder.is_dir():
            parts += [f"{p.relative_to(folder)}:{sha(p.read_text(errors='replace'))}"
                      for p in sorted(folder.rglob("*")) if p.is_file() and "__pycache__" not in p.parts]
    if case.reference_patch_file and case.reference_patch_file.exists():
        parts.append(sha(case.reference_patch_file.read_text(errors="replace")))
    return sha("\n".join(parts), 16)


def check_path(es: EvalSet, case: Case) -> Path:
    return es.dir / "checks" / f"{case.id}.json"


def load_check(es: EvalSet, case: Case) -> dict[str, Any] | None:
    """The case's check, or None when it was never checked or the case changed since."""
    p = check_path(es, case)
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    return data if data.get("case_hash") == case_hash(case) else None


def check_case(es: EvalSet, case: Case, echo: Callable[[str], None] = lambda m: None) -> dict[str, Any]:
    work = workspace.eval_home() / "work" / es.name / "check" / case.id
    try:
        co, base = workspace.checkout(case, work / "repo")
        m = workspace.mirror(case)
        ref = workspace.reference_sha(case, m)
        raw, source = workspace.base_config(case, co)
        cfg = workspace.eval_config(raw)
        workspace.prepare(case, co, cfg.raw)
        overlay, runnable = score.hidden_files(case, cfg, m, base, ref)
        echo(f"{case.id}: hidden tests on base")
        on_base, _ = score.score(case, cfg, co, base, "", m, ref)
        ref_patch = git(m, "diff", "--binary", base, ref)
        refs, log = [], ""
        for i in (1, 2):
            echo(f"{case.id}: hidden tests on the reference ({i}/2)")
            on_ref, log = score.score(case, cfg, co, base, ref_patch, m, ref)
            refs.append(on_ref.get("tests") or {})
    finally:
        shutil.rmtree(work, ignore_errors=True)
    sorted_ids = score.classify(on_base.get("tests") or {}, refs)
    problems, warnings = [], []
    if not runnable:
        problems.append("no runnable hidden tests")
    elif not sorted_ids["fail_to_pass"]:
        problems.append("no hidden test fails on base and passes on the reference: the tests do not test the change")
    if sorted_ids["fails_on_reference"]:
        warnings.append(f"{len(sorted_ids['fails_on_reference'])} hidden test(s) fail on the reference too; "
                        "left out of scoring")
    if sorted_ids["flaky"]:
        warnings.append(f"{len(sorted_ids['flaky'])} flaky hidden test(s); left out of scoring")
    result = {"case": case.id, "case_hash": case_hash(case), "checked": time.time(), "config": source,
              "base_sha": base, "reference_sha": ref, "hidden_tests": runnable, "overlay": overlay,
              **sorted_ids, "problems": problems, "warnings": warnings,
              "reference_log_tail": log[-3000:] if problems else ""}
    p = check_path(es, case)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(result, indent=1))
    return result


def summary(c: dict[str, Any]) -> str:
    head = "FAIL " if c["problems"] else "ok   "
    text = (f"{head} {c['case']}: {len(c['hidden_tests'])} hidden test file(s); {len(c['fail_to_pass'])} "
            f"fail-to-pass, {len(c['pass_to_pass'])} pass-to-pass")
    for p in c["problems"] + c["warnings"]:
        text += f"\n      {p}"
    return text
