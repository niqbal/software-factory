"""Tool broker: the only path from a model to the sandbox.

Every tool call is checked against the role's permission table, executed,
redacted and logged. The model's wording never changes what is allowed, which
is what contains prompt injection from tickets or repository content.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import policy
from .adapters.base import Sandbox, ToolSpec
from .config import Config, Package
from .store import Store
from .util import any_match, redact

MAX_READ_CHARS = 60_000
MAX_OUTPUT_CHARS = 12_000
SAFE_ARGS = re.compile(r"^[\w./:@=,+\-\s\"'\[\]*]*$")
SHELL_META = re.compile(r"[;&|`$<>(){}\\\n]")
READONLY_PREFIXES = [
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "tree", "pwd", "file", "stat",
    "git diff", "git status", "git log", "git show", "git grep", "git ls-files", "git blame",
    "node --version", "npm --version", "python --version", "python3 --version", "go version",
]
DENY_WORDS = re.compile(r"\b(curl|wget|ssh|scp|nc|ncat|sudo|chmod|chown|env|printenv|"
                        r"git\s+(push|remote|config|reset|checkout|switch|rebase|commit|worktree)|"
                        r"npm\s+(install|i|add|publish|login)|pip\s+install|yarn\s+add|pnpm\s+add)\b")

ROLE_TOOLS = {
    "implementer": ["list_files", "read_file", "search", "write_file", "edit_file", "delete_file",
                    "run_check", "run_command", "finish"],
    "tester": ["list_files", "read_file", "search", "show_diff", "write_file", "edit_file",
               "run_check", "run_command", "finish"],
    "reviewer": ["list_files", "read_file", "search", "show_diff", "submit_review"],
    "security": ["list_files", "read_file", "search", "show_diff", "submit_review"],
    "ci_fixer": ["list_files", "read_file", "search", "show_diff", "write_file", "edit_file",
                 "run_check", "run_command", "finish"],
}


class Denied(Exception):
    pass


@dataclass
class BrokerState:
    finished: bool = False
    result: dict[str, Any] = field(default_factory=dict)
    touched: set[str] = field(default_factory=set)
    created: set[str] = field(default_factory=set)
    calls: int = 0
    denials: int = 0


class ToolBroker:
    def __init__(self, sandbox: Sandbox, cfg: Config, role: str, tier: int,
                 store: Store | None = None, job_id: str = "",
                 on_event: Callable[[str, dict], None] | None = None,
                 grants: list[str] | set[str] | None = None):
        self.sb, self.cfg, self.role, self.tier = sandbox, cfg, role, tier
        # Existing test files a human allowed this job to edit (asf allow). A grant lifts only
        # the existing-test rule for those exact paths; every other check still applies.
        self.grants = set(grants or ())
        self.store, self.job_id = store, job_id
        self.on_event = on_event
        self.state = BrokerState()
        perms = cfg.get("permissions", {})
        self.protected = perms.get("protected_paths", [])
        self.secret_files = perms.get("secret_files", [])
        self.clone_dirs = cfg.get("execution.clone_dirs", [])
        self.extra_allowed = perms.get("extra_allowed_commands", [])
        self.extra_denied = [re.compile(p) for p in perms.get("denied_command_patterns", [])]
        self.max_touch = perms.get("ci_fixer", {}).get("max_files_touched", 5)
        self.timeout = cfg.get("execution.command_timeout_s", 900)

    # ------------------------------------------------------------------ tools --
    def tools(self) -> list[ToolSpec]:
        specs = {s.name: s for s in _all_specs(self.cfg)}
        return [specs[n] for n in ROLE_TOOLS[self.role]]

    def call(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        self.state.calls += 1
        if name not in ROLE_TOOLS[self.role]:
            return self._deny(name, args, f"tool '{name}' is not available to the {self.role} role")
        try:
            out = getattr(self, f"t_{name}")(**(args or {}))
            self._log(name, args, "allow", "")
            return redact(out)[:MAX_OUTPUT_CHARS * 2], False
        except Denied as e:
            return self._deny(name, args, str(e))
        except TypeError as e:
            return f"Bad arguments for {name}: {e}", True
        except FileNotFoundError as e:
            return f"Not found: {e}", True
        except Exception as e:  # noqa: BLE001 - tool errors go back to the model
            self._log(name, args, "error", str(e)[:300])
            return f"Error: {e}", True

    # --------------------------------------------------------------- checks --
    def _rel(self, path: str) -> str:
        if not path or path.startswith("/") or path.startswith("~"):
            raise Denied(f"use a repository-relative path, got '{path}'")
        root = self.sb.root.resolve()
        target = (root / path).resolve()
        if target != root and not str(target).startswith(str(root) + "/"):
            raise Denied(f"path escapes the repository: {path}")
        rel = str(target.relative_to(root)) if target != root else "."
        if rel == ".git" or rel.startswith(".git/"):
            raise Denied("the .git directory is off limits")
        if any(rel == d or rel.startswith(d.rstrip("/") + "/") for d in self.clone_dirs):
            raise Denied(f"{rel} is a dependency folder; changing dependencies needs a human")
        return rel

    def _check_read(self, rel: str) -> None:
        if any_match(rel, self.secret_files):
            raise Denied(f"{rel} matches a secret-file pattern and cannot be read")

    def _check_write(self, rel: str, deleting: bool = False) -> None:
        self._check_read(rel)
        if any_match(rel, self.protected):
            raise Denied(f"{rel} is protected by permissions.protected_paths")
        is_test = self.cfg.is_test_path(rel)
        existed = self.sb.file_at_base(rel) is not None
        if self.role in ("reviewer", "security"):
            raise Denied("reviewers cannot modify files")
        if self.role == "tester" and not is_test:
            raise Denied(f"the tester may only write test files; {rel} is not a test path "
                         f"(test globs: {', '.join(self.cfg.test_globs[:6])})")
        if is_test and existed:
            if deleting:
                raise Denied(f"deleting existing test {rel} is never allowed")
            if not policy.may_edit_existing_tests(self.cfg, self.tier) and rel not in self.grants:
                raise Denied(f"{rel} is an existing test (it was in the repository when this job "
                             f"started); at tier {self.tier} agents add tests rather than edit existing "
                             "ones. Test files this job created are not affected. A human can allow "
                             "this file for this job with asf allow.")
        if self.role == "ci_fixer" and rel not in self.state.touched \
                and len(self.state.touched) >= self.max_touch:
            raise Denied(f"ci_fixer may touch at most {self.max_touch} files")

    def _check_command(self, command: str) -> None:
        if SHELL_META.search(command):
            raise Denied("shell operators (; & | ` $ < > parentheses) are not allowed in run_command; "
                         "use run_check for configured commands or the search tool")
        if DENY_WORDS.search(command) or any(p.search(command) for p in self.extra_denied):
            raise Denied(f"command not permitted: {command}")
        normalized = " ".join(shlex.split(command))
        allowed = READONLY_PREFIXES + list(self.extra_allowed)
        if not any(normalized == p or normalized.startswith(p + " ") for p in allowed):
            raise Denied("run_command only allows read-only inspection commands "
                         f"({', '.join(READONLY_PREFIXES[:8])}, ...) plus permissions.extra_allowed_commands. "
                         "Use run_check to run tests, lint, typecheck or build.")

    # -------------------------------------------------------------- read ops --
    def t_list_files(self, path: str = ".", pattern: str = "") -> str:
        rel = self._rel(path)
        res = self.sb.exec("git ls-files --cached --others --exclude-standard -- " + shlex.quote(rel))
        files = [f for f in res.stdout.splitlines() if not pattern or any_match(f, [pattern])]
        files = [f for f in files if not any(f.startswith(d.rstrip('/') + '/') for d in self.clone_dirs)]
        head = files[:400]
        more = f"\n... {len(files) - 400} more" if len(files) > 400 else ""
        return "\n".join(head) + more if head else "(no files)"

    def t_read_file(self, path: str, offset: int = 1, limit: int = 400) -> str:
        rel = self._rel(path)
        self._check_read(rel)
        p = self.sb.root / rel
        if p.is_dir():
            return self.t_list_files(rel)
        lines = p.read_text(errors="replace").splitlines()
        start = max(offset, 1)
        chunk = lines[start - 1:start - 1 + max(1, min(limit, 2000))]
        body = "\n".join(f"{i:>5}  {line}" for i, line in enumerate(chunk, start))
        if len(body) > MAX_READ_CHARS:
            body = body[:MAX_READ_CHARS] + "\n...[truncated]"
        tail = f"\n[lines {start}-{start + len(chunk) - 1} of {len(lines)}]"
        return body + tail

    def t_search(self, pattern: str, path_glob: str = "") -> str:
        args = ["git", "grep", "-n", "-I", "-E", "--untracked", "-e", pattern]
        if path_glob:
            args += ["--", path_glob]
        res = self.sb.exec(" ".join(shlex.quote(a) for a in args))
        lines = [ln for ln in res.stdout.splitlines() if not any_match(ln.split(":", 1)[0], self.secret_files)]
        if not lines:
            return "(no matches)"
        return "\n".join(lines[:200]) + (f"\n... {len(lines) - 200} more" if len(lines) > 200 else "")

    def t_show_diff(self, path: str = "") -> str:
        diff = self.sb.diff([self._rel(path)] if path else None)
        return diff[:MAX_READ_CHARS] or "(no changes)"

    # ------------------------------------------------------------- write ops --
    def t_write_file(self, path: str, content: str) -> str:
        rel = self._rel(path)
        self._check_write(rel)
        p = self.sb.root / rel
        created = not p.exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        self.state.touched.add(rel)
        if created:
            self.state.created.add(rel)
        return f"{'created' if created else 'wrote'} {rel} ({len(content.splitlines())} lines)"

    def t_edit_file(self, path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
        rel = self._rel(path)
        self._check_write(rel)
        p = self.sb.root / rel
        text = p.read_text()
        count = text.count(old_text)
        if count == 0:
            raise ValueError("old_text not found; read the file again and copy the exact text")
        if count > 1 and not replace_all:
            raise ValueError(f"old_text appears {count} times; include more context or set replace_all")
        p.write_text(text.replace(old_text, new_text) if replace_all else text.replace(old_text, new_text, 1))
        self.state.touched.add(rel)
        return f"edited {rel} ({count if replace_all else 1} replacement)"

    def t_delete_file(self, path: str) -> str:
        rel = self._rel(path)
        self._check_write(rel, deleting=True)
        (self.sb.root / rel).unlink()
        self.state.touched.add(rel)
        return f"deleted {rel}"

    # --------------------------------------------------------------- run ops --
    def _package(self, name: str | None, kind: str) -> Package:
        pkgs = [p for p in self.cfg.packages if kind in p.commands]
        if not pkgs:
            raise Denied(f"no package defines a '{kind}' command in factory.yaml")
        if name:
            for p in pkgs:
                if p.name == name:
                    return p
            raise Denied(f"package '{name}' has no '{kind}' command; options: {[p.name for p in pkgs]}")
        if len(pkgs) > 1:
            raise Denied(f"several packages define '{kind}'; pass package as one of {[p.name for p in pkgs]}")
        return pkgs[0]

    def t_run_check(self, kind: str, package: str = "", args: str = "") -> str:
        if kind not in ("test", "lint", "typecheck", "build"):
            raise Denied("kind must be test, lint, typecheck or build")
        pkg = self._package(package or None, kind)
        if args and (not SAFE_ARGS.match(args) or SHELL_META.search(args)):
            raise Denied("args may contain only paths, test names and simple flags")
        template = pkg.commands[kind]
        command = template.replace("{args}", args) if "{args}" in template else \
            (template + (" " + args if args else ""))
        res = self.sb.exec(command, cwd=pkg.root, timeout=self.timeout)
        status = "PASSED" if res.ok else ("TIMED OUT" if res.timed_out else f"FAILED (exit {res.code})")
        return f"$ {command}  [cwd {pkg.root}]\n{status} in {res.duration:.0f}s\n{res.tail(MAX_OUTPUT_CHARS)}"

    def t_run_command(self, command: str, cwd: str = ".") -> str:
        self._check_command(command)
        rel = self._rel(cwd)
        res = self.sb.exec(command, cwd=rel, timeout=120)
        return f"exit {res.code}\n{res.tail(MAX_OUTPUT_CHARS)}"

    # --------------------------------------------------------------- finish --
    def t_finish(self, summary: str, **extra: Any) -> str:
        self.state.finished = True
        self.state.result = {"summary": summary, **extra}
        return "Recorded. Stop now."

    def t_submit_review(self, verdict: str, summary: str, findings: list[dict] | None = None) -> str:
        if verdict not in ("approve", "request_changes"):
            raise ValueError("verdict must be approve or request_changes")
        self.state.finished = True
        self.state.result = {"verdict": verdict, "summary": summary, "findings": findings or []}
        return "Review recorded. Stop now."

    # ---------------------------------------------------------------- logging --
    def _deny(self, name: str, args: dict, reason: str) -> tuple[str, bool]:
        self.state.denials += 1
        self._log(name, args, "deny", reason)
        return f"DENIED by policy: {reason}", True

    def _log(self, name: str, args: dict, decision: str, reason: str) -> None:
        summary = {k: (v if not isinstance(v, str) or len(v) < 200 else v[:200] + f"...({len(v)} chars)")
                   for k, v in (args or {}).items()}
        payload = {"role": self.role, "tool": name, "args": summary, "decision": decision, "reason": reason}
        if self.store and self.job_id:
            self.store.event(self.job_id, "tool.call", self.role, payload)
        if self.on_event:
            self.on_event("tool.call", payload)


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def _all_specs(cfg: Config) -> list[ToolSpec]:
    pkgs = {p.name: sorted(p.commands) for p in cfg.packages}
    pkg_note = "; ".join(f"{n}: {', '.join(k)}" for n, k in pkgs.items()) or "none configured"
    finding = _obj({"severity": {"type": "string", "enum": ["blocking", "major", "minor", "nit"]},
                    "file": {"type": "string"}, "line": {"type": "integer"},
                    "issue": {"type": "string"}}, ["severity", "file", "issue"])
    return [
        ToolSpec("list_files", "List tracked and new files under a directory, optionally filtered by a glob.",
                 _obj({"path": {"type": "string"}, "pattern": {"type": "string"}}, [])),
        ToolSpec("read_file", "Read a text file with line numbers. Use offset/limit for long files.",
                 _obj({"path": {"type": "string"}, "offset": {"type": "integer"},
                       "limit": {"type": "integer"}}, ["path"])),
        ToolSpec("search", "Search file contents with an extended regex (git grep). Optional path glob.",
                 _obj({"pattern": {"type": "string"}, "path_glob": {"type": "string"}}, ["pattern"])),
        ToolSpec("show_diff", "Show the current diff against the base commit, optionally for one path.",
                 _obj({"path": {"type": "string"}}, [])),
        ToolSpec("write_file", "Create or overwrite a file with the full content.",
                 _obj({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"])),
        ToolSpec("edit_file", "Replace exact text in a file. old_text must match once unless replace_all.",
                 _obj({"path": {"type": "string"}, "old_text": {"type": "string"},
                       "new_text": {"type": "string"}, "replace_all": {"type": "boolean"}},
                      ["path", "old_text", "new_text"])),
        ToolSpec("delete_file", "Delete a file.", _obj({"path": {"type": "string"}}, ["path"])),
        ToolSpec("run_check", f"Run a configured project command. Packages and commands: {pkg_note}. "
                              "args are appended (e.g. a test file path or -t 'name').",
                 _obj({"kind": {"type": "string", "enum": ["test", "lint", "typecheck", "build"]},
                       "package": {"type": "string"}, "args": {"type": "string"}}, ["kind"])),
        ToolSpec("run_command", "Run a read-only inspection command such as ls, git log or git diff.",
                 _obj({"command": {"type": "string"}, "cwd": {"type": "string"}}, ["command"])),
        ToolSpec("finish", "Call once when your work is complete.",
                 _obj({"summary": {"type": "string"},
                       "acceptance_met": {"type": "array", "items": {"type": "string"}},
                       "tests_added": {"type": "array", "items": {"type": "string"}},
                       "tests_pass": {"type": "boolean"},
                       "defect_found": {"type": "string"},
                       "blocked_reason": {"type": "string"}}, ["summary"])),
        ToolSpec("submit_review", "Submit your review verdict and findings. Call exactly once.",
                 _obj({"verdict": {"type": "string", "enum": ["approve", "request_changes"]},
                       "summary": {"type": "string"},
                       "findings": {"type": "array", "items": finding}}, ["verdict", "summary"])),
    ]
