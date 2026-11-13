"""Scoring: hidden tests, size against the reference, and the job's own numbers.

The hidden tests are the test files the reference commit added or changed. To score a
candidate (the factory's branch, or what the interactive baseline left in its checkout), the
checkout goes back to the base commit, the candidate's patch is applied, the reference's versions
of the hidden test files are written over it (as SWE-bench does: where the candidate edited the
same test file, the reference's version wins), and each hidden test file runs on its own.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from ..config import Config
from ..store import Store
from ..util import any_match, git, run, sandbox_env
from . import workspace
from .cases import Case

# Files a test runner can be pointed at. Other changed test files (conftest.py, fixtures,
# __init__.py, snapshots) are written over the candidate but not run on their own.
RUNNABLE = ["**/test_*.py", "**/*_test.py", "**/*.test.*", "**/*.spec.*", "**/*_test.go",
            "**/*Test.java", "**/*Test.kt", "**/*_spec.rb", "**/*Tests.swift", "**/*_test.rs"]


def changed_files(repo: Path, base: str, ref: str) -> list[tuple[str, str]]:
    """(status, path) for every file that differs between two commits (renames as delete + add)."""
    out = git(repo, "diff", "--name-status", "--no-renames", base, ref)
    rows = []
    for line in out.splitlines():
        status, _, path = line.partition("\t")
        if path:
            rows.append((status[:1], path))
    return rows


def hidden_overlay(case: Case, cfg: Config, mirror: Path, base: str, ref: str) -> dict[str, bytes | None]:
    """Hidden files to write over a candidate: path -> content (None deletes the file).

    From the case's authored folder (`hidden_dir`) when it has one, else the test files the
    reference adds or changes (or the `hidden_tests` the case lists)."""
    folder = case.hidden_folder
    if folder is not None:
        if not folder.is_dir():
            from .cases import EvalError
            raise EvalError(f"case {case.id}: hidden_dir {folder} does not exist")
        return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*"))
                if p.is_file() and "__pycache__" not in p.parts}
    paths = list(case.hidden_tests) or [p for _, p in changed_files(mirror, base, ref) if cfg.is_test_path(p)]
    return {p: file_at(mirror, ref, p) for p in paths}


def hidden_files(case: Case, cfg: Config, mirror: Path, base: str, ref: str) -> tuple[list[str], list[str]]:
    """(files written over the candidate, files run). The first includes deletions and helpers."""
    overlay = hidden_overlay(case, cfg, mirror, base, ref)
    present = [p for p, c in overlay.items() if c is not None]
    if case.hidden_tests and case.hidden_folder is not None:
        runnable = [p for p in case.hidden_tests if p in present]
    else:
        runnable = [p for p in present if any_match(p, RUNNABLE)]
    if not runnable and case.hidden_tests:
        runnable = [p for p in case.hidden_tests if p in present]
    return list(overlay), runnable


def file_at(repo: Path, ref: str, path: str) -> bytes | None:
    p = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{path}"], capture_output=True)
    return p.stdout if p.returncode == 0 else None


def numstat(patch: str, cfg: Config) -> dict[str, Any]:
    """Lines and files of a patch, split into code and tests."""
    if not patch.strip():
        return {"files": [], "code_files": [], "code_lines": 0, "test_lines": 0}
    p = subprocess.run(["git", "apply", "--numstat", "-"], input=patch, capture_output=True, text=True)
    files, code_files, code, tests = [], [], 0, 0
    for line in p.stdout.splitlines():
        added, deleted, path = (line.split("\t", 2) + ["", ""])[:3]
        n = (int(added) if added.isdigit() else 0) + (int(deleted) if deleted.isdigit() else 0)
        files.append(path)
        if cfg.is_test_path(path):
            tests += n
        else:
            code += n
            code_files.append(path)
    return {"files": files, "code_files": code_files, "code_lines": code, "test_lines": tests}


def compare(candidate: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    ref_code = set(reference["code_files"])
    cand_code = set(candidate["code_files"])
    return {
        **candidate,
        "ref_files": reference["files"], "ref_code_lines": reference["code_lines"],
        "ref_test_lines": reference["test_lines"],
        # >1: the candidate changed more code than the merged solution did.
        "size_ratio": round(candidate["code_lines"] / reference["code_lines"], 2) if reference["code_lines"] else None,
        # Share of the files the reference changed that the candidate changed too.
        "ref_files_touched": round(len(ref_code & cand_code) / len(ref_code), 2) if ref_code else None,
        "extra_files": sorted(cand_code - ref_code),
    }


# ----------------------------------------------------------------------------- running tests

COUNT = {
    "passed": re.compile(r"(\d+) passed"), "failed": re.compile(r"(\d+) failed"),
    "error": re.compile(r"(\d+) errors?\b"),
}


def parse_counts(output: str, ok: bool) -> tuple[int, int]:
    """(passed, total) from a test runner's summary; one unit for the whole file when unknown."""
    text = output[-4000:]
    go_pass, go_fail = len(re.findall(r"^\s*--- PASS", text, re.M)), len(re.findall(r"^\s*--- FAIL", text, re.M))
    if go_pass or go_fail:
        return go_pass, go_pass + go_fail
    summary = ""
    for line in reversed(text.splitlines()):
        if re.search(r"\d+ (passed|failed)", line):
            summary = line
            break
    if summary:
        passed = int(m.group(1)) if (m := COUNT["passed"].search(summary)) else 0
        failed = int(m.group(1)) if (m := COUNT["failed"].search(summary)) else 0
        errors = int(m.group(1)) if (m := COUNT["error"].search(summary)) else 0
        failed += errors
        if not ok and failed == 0:
            failed = 1  # the runner failed without saying which test: count it once
        return passed, passed + failed
    return (1, 1) if ok else (0, 1)


def command_for(case: Case, cfg: Config, path: str) -> tuple[str, str] | None:
    """(command, folder relative to the checkout) that runs one hidden test file."""
    if case.test_command:
        cmd = case.test_command
        folder = str(Path(path).parent)
        repl = {"{tests}": path, "{args}": path, "{dirs}": folder, "{pkgs}": "./" + folder}
        if not any(k in cmd for k in repl):
            return verbose(f"{cmd} {path}"), "."
        for k, v in repl.items():
            cmd = cmd.replace(k, v)
        return verbose(cmd), "."
    owners = [p for p in cfg.packages if p.commands.get("test") and
              (p.root in ("", ".") or path.startswith(p.root.rstrip("/") + "/"))]
    if not owners:
        return None
    pkg = max(owners, key=lambda p: len(p.root.strip(".")))
    root = "" if pkg.root in ("", ".") else pkg.root.rstrip("/") + "/"
    rel = path[len(root):]
    cmd = pkg.commands["test"]
    if rel.endswith("_test.go"):
        # go test takes packages, not files; the whole package runs (pass-to-pass covers the rest).
        rel = "./" + (str(Path(rel).parent) if str(Path(rel).parent) != "." else "")
        cmd = cmd.replace("./...", "")
    cmd = cmd.replace("{args}", rel) if "{args}" in cmd else f"{cmd} {rel}"
    return verbose(cmd), pkg.root or "."


def verbose(cmd: str) -> str:
    """Ask the runner to name every test it ran, so results can be read per test."""
    if "pytest" in cmd and " -rA" not in cmd:
        return cmd + " -rA"
    if re.search(r"\bgo test\b|/go test\b", cmd) and " -v" not in cmd:
        return cmd.replace(" test ", " test -v ", 1)
    if "vitest" in cmd and "--reporter" not in cmd:
        return cmd + " --reporter=verbose"
    if "jest" in cmd and "--verbose" not in cmd:
        return cmd + " --verbose"
    return cmd


PYTEST_ID = re.compile(r"^(PASSED|FAILED|ERROR|XPASS|XFAIL|SKIPPED)\s+(\S+)", re.M)
GO_ID = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)", re.M)
JS_ID = re.compile(r"^\s*([✓✔√×✕✗])\s+(.+?)(?:\s+\d+\s*m?s)?\s*$", re.M)


def test_ids(output: str, file: str) -> dict[str, bool]:
    """Test id -> passed, read from a runner's verbose output. Empty when nothing is named."""
    out: dict[str, bool] = {}
    for status, tid in PYTEST_ID.findall(output):
        if status == "SKIPPED":
            continue
        out[tid] = status in ("PASSED", "XFAIL")
    if out:
        return out
    for status, tid in GO_ID.findall(output):
        if status != "SKIP":
            out[f"{file}::{tid}"] = status == "PASS"
    if out:
        return out
    for mark, tid in JS_ID.findall(output):
        out[f"{file}::{tid.strip()}"] = mark in "✓✔√"
    return out


def run_hidden(case: Case, cfg: Config, root: Path, runnable: list[str]) -> tuple[dict[str, Any], str]:
    """Run each hidden test file (each go package once). Returns per-file and per-test results."""
    timeout = cfg.get("execution.command_timeout_s", 900)
    env = sandbox_env(cfg.get("execution.env"))
    files, log, tests, seen = [], [], {}, set()
    for path in runnable:
        how = command_for(case, cfg, path)
        if not how:
            files.append({"file": path, "ok": False, "passed": 0, "total": 1,
                          "error": "no package test command covers this file; set test_command in the case"})
            tests[path] = False
            continue
        if how in seen:
            continue
        seen.add(how)
        cmd, folder = how
        res = run(cmd, cwd=root / folder, timeout=timeout, env=env)
        output = res.stdout + "\n" + res.stderr
        passed, total = parse_counts(output, res.ok)
        ids = test_ids(output, path)
        if not ids:
            ids = {path: res.ok}  # nothing named: the file is one test
        elif not res.ok and all(ids.values()):
            ids[f"{path}::(run)"] = False  # failed outside any named test (build, collection, setup)
        tests.update(ids)
        files.append({"file": path, "ok": res.ok, "passed": passed, "total": total, "command": cmd,
                      "seconds": round(res.duration, 1)})
        log.append(f"$ (cd {folder} && {cmd})  -> exit {res.code}\n{res.tail(8000)}\n")
    passed = sum(f["passed"] for f in files)
    total = sum(f["total"] for f in files)
    return {"files": files, "tests": tests, "passed": passed, "total": total,
            "rate": round(passed / total, 3) if total else None,
            "all_pass": bool(files) and all(f["ok"] for f in files)}, "\n".join(log)


def against_check(hidden: dict[str, Any], check: dict[str, Any] | None) -> dict[str, Any]:
    """Score per test against the case's check: fail-to-pass must pass, pass-to-pass must not break.

    Without a check (or with no fail-to-pass tests in it), the per-file result stands."""
    f2p = (check or {}).get("fail_to_pass") or []
    if not f2p:
        return {**hidden, "scored": "per file"}
    p2p = (check or {}).get("pass_to_pass") or []
    tests = hidden.get("tests") or {}
    fixed = [t for t in f2p if tests.get(t)]
    broken = [t for t in p2p if not tests.get(t)]
    return {**hidden, "scored": "per test", "fail_to_pass": len(f2p), "fixed": len(fixed),
            "pass_to_pass": len(p2p), "broken": broken[:50], "not_fixed": [t for t in f2p if not tests.get(t)][:50],
            "passed": len(fixed), "total": len(f2p), "rate": round(len(fixed) / len(f2p), 3),
            "all_pass": len(fixed) == len(f2p) and not broken}


def classify(base: dict[str, bool], refs: list[dict[str, bool]]) -> dict[str, list[str]]:
    """Fail-to-pass, pass-to-pass and flaky test ids from runs on base and (repeated) on the reference."""
    ids = set().union(*refs) if refs else set()
    flaky = sorted(t for t in ids if len({r.get(t, False) for r in refs}) > 1)
    steady = [t for t in ids if t not in flaky and all(r.get(t) for r in refs)]
    return {"fail_to_pass": sorted(t for t in steady if not base.get(t)),
            "pass_to_pass": sorted(t for t in steady if base.get(t)),
            "fails_on_reference": sorted(t for t in ids if t not in flaky and not any(r.get(t) for r in refs)),
            "flaky": flaky}


def score(case: Case, cfg: Config, root: Path, base: str, patch: str, mirror: Path, ref: str) -> tuple[dict[str, Any], str]:
    """Apply `patch` to a clean base in `root`, write the hidden tests over it, run them.

    Returns (hidden result, log); scoring per test against the case's check is against_check's
    job. `root` is left in the scored state."""
    overlay = hidden_overlay(case, cfg, mirror, base, ref)
    _, runnable = hidden_files(case, cfg, mirror, base, ref)
    workspace.reset(root, base, cfg)
    if patch.strip():
        p = subprocess.run(["git", "-C", str(root), "apply", "--binary", "--whitespace=nowarn", "-"],
                           input=patch.encode(), capture_output=True)
        if p.returncode != 0:
            err = p.stderr.decode(errors="replace")[:1500]
            return {"files": [], "tests": {}, "passed": 0, "total": len(runnable) or 1, "rate": 0.0,
                    "all_pass": False, "error": f"the candidate patch does not apply: {err}"}, err
    for path, content in overlay.items():
        target = root / path
        if content is None:
            if target.exists():
                target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    if not runnable:
        return {"files": [], "tests": {}, "passed": 0, "total": 0, "rate": None, "all_pass": False,
                "error": "the case has no runnable hidden tests"}, ""
    return run_hidden(case, cfg, root, runnable)


# ----------------------------------------------------------------------------- the job's numbers

def job_metrics(store: Store, job_id: str) -> dict[str, Any]:
    """What the factory's own records say about a job: gates, fix rounds, denials, spend."""
    job = store.job(job_id)
    d = job["data"]
    events = list(store.events(job_id))
    gates = d.get("gates") or []
    delivered = bool(d.get("delivered_at"))
    # The first verification round: gate results until the CI fixer first starts.
    first: list[dict[str, Any]] = []
    started = False
    for e in events:
        if e["type"] == "gate.result":
            started = True
            first.append(e["payload"])
        elif started and e["type"] == "agent.start" and e["actor"] == "ci_fixer":
            break
        elif started and e["type"] == "job.state":
            break
    denials = sum(1 for e in events if e["type"] == "tool.call" and e["payload"].get("decision") == "deny")
    return {
        "job_id": job_id, "final_state": job["state"], "tier": job["tier"], "delivered": delivered,
        "visible_pass": delivered and bool(gates) and not any(g.get("status") == "fail" for g in gates),
        "gates": [{"name": g.get("name"), "package": g.get("package"), "status": g.get("status"),
                   "detail": str(g.get("detail") or "")[:300]} for g in gates],
        "gates_first_try": (not any(g.get("status") == "fail" for g in first)) if first else None,
        "fix_rounds": int(d.get("tester_rounds", 0)) + int(d.get("review_rounds", 0)) + int(d.get("ci_rounds", 0)),
        "rounds": {"tester": d.get("tester_rounds", 0), "review": d.get("review_rounds", 0),
                   "ci": d.get("ci_rounds", 0)},
        "review_blocking": len(d.get("unresolved_review") or []),
        "denials": denials,
        "cost_usd": round(store.job_cost(job_id), 4),
        "auto_retries": int(d.get("auto_retries", 0)),
        "stop_reason": d.get("needs_human_reason") or d.get("paused_reason") or d.get("error") or "",
        "branch": (d.get("delivery") or {}).get("branch") or d.get("branch"),
        "commit": d.get("commit"),
    }
