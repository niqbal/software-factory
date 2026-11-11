"""Deterministic gates. They run before any LLM review and cannot be argued with.

Command gates (lint, typecheck, build, test) run only for packages the diff
touches. When one fails, the same command runs on the base commit; failures that
already exist there are reported as "preexisting" instead of blamed on the change.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

from . import policy
from .adapters.base import Sandbox
from .config import Config
from .models import GateResult
from .util import ExecResult, find_secrets

LOCKFILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Cargo.lock",
             "go.sum", "Gemfile.lock", "composer.lock", "uv.lock", "bun.lockb"}
CODE_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".go", ".rs", ".java", ".kt",
            ".rb", ".php", ".cs", ".swift", ".scala", ".c", ".cc", ".cpp", ".h"}
COMMAND_GATES = ("lint", "typecheck", "build", "test")

FAIL_PATTERNS = [
    re.compile(r"^\s*FAIL\s+(\S+)", re.M),                          # jest / vitest file
    re.compile(r"^\s*[×✗]\s+(.+?)(?:\s+\d+ms)?$", re.M),              # vitest test line
    re.compile(r"^FAILED\s+(\S+?)(?:\s+-\s.*)?$", re.M),               # pytest
    re.compile(r"^\s*--- FAIL: (\S+)", re.M),                         # go test, including subtests
    re.compile(r"^(\S+?)\(\d+,\d+\): error (TS\d+)", re.M),           # tsc
    re.compile(r"^(\S+?):\d+:\d+: error (TS\d+)", re.M),              # tsc --pretty false variants
    re.compile(r"^(\S+\.\w+):\d+:\d+:\s+(\w[\w-]*)\s", re.M),          # ruff / eslint unix format
]


def failing_ids(output: str) -> set[str]:
    ids: set[str] = set()
    for pat in FAIL_PATTERNS:
        for m in pat.finditer(output):
            ids.add(":".join(g for g in m.groups() if g).strip())
    return ids


def is_code(path: str) -> bool:
    return Path(path).suffix in CODE_EXT


def added_lines(diff: str) -> str:
    return "\n".join(line[1:] for line in diff.splitlines()
                     if line.startswith("+") and not line.startswith("+++"))


def _deps_from_package_json(text: str | None) -> set[str]:
    if not text:
        return set()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return set()
    out = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        out |= set((data.get(key) or {}).keys())
    return out


def _deps_from_lines(text: str | None, pattern: str) -> set[str]:
    if not text:
        return set()
    return {m.group(1).lower() for m in re.finditer(pattern, text, re.M)}


DEP_PARSERS: dict[str, Callable[[str | None], set[str]]] = {
    "package.json": _deps_from_package_json,
    "requirements.txt": lambda t: _deps_from_lines(t, r"^\s*([A-Za-z0-9_.\-]+)"),
    "pyproject.toml": lambda t: _deps_from_lines(t, r"^\s*\"([A-Za-z0-9_.\-]+)[^\"]*\",?\s*$"),
    "go.mod": lambda t: _deps_from_lines(t, r"^\s*(?:require\s+)?([a-z0-9.\-]+\.[a-z]+/\S+)\s+v"),
    "Cargo.toml": lambda t: _deps_from_lines(t, r"^([a-zA-Z0-9_\-]+)\s*=\s*[\"{]"),
    "Gemfile": lambda t: _deps_from_lines(t, r"^\s*gem\s+['\"]([^'\"]+)"),
}


class GateRunner:
    def __init__(self, cfg: Config, sandbox: Sandbox, tier: int, state_dir: Path,
                 base_sandbox: Callable[[], Sandbox] | None = None,
                 log: Callable[[str, dict], None] | None = None):
        self.cfg, self.sb, self.tier, self.state_dir = cfg, sandbox, tier, state_dir
        self.base_sandbox = base_sandbox
        self.log = log or (lambda *_: None)
        self.timeout = cfg.get("execution.command_timeout_s", 900)

    # ------------------------------------------------------------------ api --
    def run(self, names: list[str]) -> list[GateResult]:
        changed = self.sb.changed_files()
        diff = self.sb.diff()
        results: list[GateResult] = []
        for name in names:
            if name in COMMAND_GATES:
                results.extend(self.command_gate(name, changed))
            elif hasattr(self, f"g_{name}"):
                results.append(getattr(self, f"g_{name}")(changed, diff))
            else:
                results.append(GateResult(name, "skipped", "unknown gate"))
        for r in results:
            self.log("gate.result", {"gate": r.name, "package": r.package, "status": r.status,
                                     "detail": r.detail})
        return results

    # -------------------------------------------------------------- static --
    def g_secret_scan(self, changed: list[str], diff: str) -> GateResult:
        hits = find_secrets(added_lines(diff))
        secret_paths = [p for p in changed if re.search(r"(^|/)\.env(\.|$)|\.pem$|\.key$", p)]
        if hits or secret_paths:
            return GateResult("secret_scan", "fail", f"possible secrets added: {hits[:5]} {secret_paths[:5]}")
        return GateResult("secret_scan", "pass", "no secrets in added lines")

    def g_diff_size(self, changed: list[str], diff: str) -> GateResult:
        limit = self.cfg.get(f"limits.max_changed_lines.tier_{self.tier}", 1200)
        total = sum(a + d for a, d, p in self.sb.numstat() if Path(p).name not in LOCKFILES)
        status = "pass" if total <= limit else "fail"
        return GateResult("diff_size", status, f"{total} changed lines (limit {limit} at tier {self.tier})")

    def g_test_integrity(self, changed: list[str], diff: str) -> GateResult:
        problems = []
        may_edit = policy.may_edit_existing_tests(self.cfg, self.tier)
        for path in changed:
            if not self.cfg.is_test_path(path):
                continue
            base = self.sb.file_at_base(path)
            if base is None:
                continue
            current = self.sb.root / path
            if not current.exists():
                problems.append(f"deleted existing test {path}")
            elif not may_edit:
                problems.append(f"modified existing test {path}")
            else:
                removed = len(re.findall(r"\b(expect|assert|should)\b", base))
                kept = len(re.findall(r"\b(expect|assert|should)\b", current.read_text(errors="replace")))
                if kept < removed:
                    problems.append(f"{path} lost {removed - kept} assertions")
        code_changed = [p for p in changed if is_code(p) and not self.cfg.is_test_path(p)]
        tests_changed = [p for p in changed if self.cfg.is_test_path(p)]
        if (self.cfg.get("limits.require_tests_for_code_changes", True) and self.tier >= 1
                and code_changed and not tests_changed):
            problems.append("code changed but no tests were added or updated")
        if problems:
            return GateResult("test_integrity", "fail", "; ".join(problems))
        return GateResult("test_integrity", "pass", f"{len(tests_changed)} test files added or extended")

    def g_dependency_policy(self, changed: list[str], diff: str) -> GateResult:
        added: list[str] = []
        for path in changed:
            parser = DEP_PARSERS.get(Path(path).name)
            if not parser:
                continue
            cur = self.sb.root / path
            new = parser(cur.read_text() if cur.exists() else None) - parser(self.sb.file_at_base(path))
            added += [f"{path}: {d}" for d in sorted(new)]
        if not added:
            return GateResult("dependency_policy", "pass", "no new dependencies")
        if policy.new_dependencies_allowed(self.cfg, self.tier):
            return GateResult("dependency_policy", "pass", f"new dependencies (flagged for review): {added}")
        return GateResult("dependency_policy", "fail",
                          f"new dependencies are not allowed at tier {self.tier}: {added}")

    # ------------------------------------------------------------- commands --
    def command_gate(self, kind: str, changed: list[str]) -> list[GateResult]:
        pkgs = [p for p in self.cfg.packages if kind in p.commands and any(p.owns(c) for c in changed)]
        code_changed = any(is_code(c) for c in changed)
        if not pkgs:
            if kind == "test" and code_changed and self.tier >= 1:
                return [GateResult("test", "fail", "code changed but no configured test command covers "
                                                   "the changed paths")]
            return [GateResult(kind, "skipped", "no configured package command covers the changed paths")]
        results = []
        for pkg in pkgs:
            cmd = pkg.commands[kind].replace("{args}", "").strip()
            res = self.sb.exec(cmd, cwd=pkg.root, timeout=self.timeout)
            if res.ok:
                results.append(GateResult(kind, "pass", f"{cmd} ({res.duration:.0f}s)", package=pkg.name))
                continue
            results.append(self._compare_with_base(kind, pkg.name, pkg.root, cmd, res))
        return results

    def _compare_with_base(self, kind: str, pkg: str, root: str, cmd: str, res: ExecResult) -> GateResult:
        if res.timed_out:
            return GateResult(kind, "fail", f"{cmd} timed out", res.tail(4000), pkg)
        base = self._baseline(root, cmd)
        if base is None:
            return GateResult(kind, "fail", f"{cmd} failed (exit {res.code})", res.tail(4000), pkg)
        now_ids = failing_ids(res.stdout + "\n" + res.stderr)
        if base["ok"]:
            return GateResult(kind, "fail", f"{cmd} fails on this change but passes on base", res.tail(4000), pkg)
        base_ids = set(base["ids"])
        new = now_ids - base_ids
        if now_ids and not new:
            return GateResult(kind, "preexisting",
                              f"{cmd} also fails on base; {len(now_ids)} failures all pre-existing", "", pkg)
        if not now_ids:
            return GateResult(kind, "fail", f"{cmd} fails here and on base, and failures could not be "
                                            "parsed to tell them apart", res.tail(4000), pkg)
        return GateResult(kind, "fail", f"{cmd}: {len(new)} new failures: {sorted(new)[:8]}", res.tail(4000), pkg)

    def _baseline(self, root: str, cmd: str) -> dict | None:
        if not self.base_sandbox:
            return None
        cache_path = self.state_dir / "baseline" / f"{self.sb.base_sha}.json"
        cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        key = f"{root}::{cmd}"
        if key not in cache:
            base_sb = self.base_sandbox()
            res = base_sb.exec(cmd, cwd=root, timeout=self.timeout)
            cache[key] = {"ok": res.ok, "ids": sorted(failing_ids(res.stdout + "\n" + res.stderr)),
                          "tail": res.tail(1500)}
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, indent=1))
            self.log("gate.baseline", {"command": cmd, "ok": res.ok, "failures": len(cache[key]["ids"])})
        return cache[key]
