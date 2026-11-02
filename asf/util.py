"""Small shared helpers: glob matching, subprocess, redaction, ids, time."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def now() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else now()))


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(3)}"


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "item"


def sha(text: str, n: int = 12) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:n]


# ---------------------------------------------------------------------------
# Glob matching with ** support (pathlib.full_match only exists on 3.13+).
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1024)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    i, out = 0, []
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def glob_match(path: str, pattern: str) -> bool:
    path = path.lstrip("./") if path.startswith("./") else path
    if _glob_regex(pattern).match(path):
        return True
    # A bare filename pattern (no slash) matches the basename anywhere, like .gitignore.
    if "/" not in pattern:
        return fnmatch.fnmatch(path.rsplit("/", 1)[-1], pattern)
    return False


def any_match(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    return any(glob_match(path, p) for p in patterns)


# ---------------------------------------------------------------------------
# Subprocess
# ---------------------------------------------------------------------------

@dataclass
class ExecResult:
    code: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out

    def tail(self, chars: int = 6000) -> str:
        text = (self.stdout + ("\n" + self.stderr if self.stderr else "")).strip()
        return text if len(text) <= chars else "...[truncated]...\n" + text[-chars:]


SAFE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "SHELL", "TERM")


def sandbox_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Minimal environment for commands run on behalf of agents.

    Credentials in the parent process (API keys, tokens) are deliberately not
    inherited, so an agent cannot read them with `env` or leak them in output.
    """
    env = {k: os.environ[k] for k in SAFE_ENV_KEYS if k in os.environ}
    env.update({"CI": "1", "FORCE_COLOR": "0", "NO_COLOR": "1"})
    if extra:
        env.update(extra)
    return env


def run(cmd: str | list[str], cwd: str | Path | None = None, timeout: float = 600,
        env: dict[str, str] | None = None, shell: bool | None = None) -> ExecResult:
    start = now()
    use_shell = isinstance(cmd, str) if shell is None else shell
    try:
        p = subprocess.run(cmd, cwd=cwd, shell=use_shell, capture_output=True, text=True,
                           timeout=timeout, env=env, executable="/bin/bash" if use_shell else None)
        return ExecResult(p.returncode, p.stdout, p.stderr, now() - start)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        return ExecResult(124, out, err + f"\n[timed out after {timeout}s]", now() - start, True)


def git(repo: str | Path, *args: str, check: bool = True, timeout: float = 120) -> str:
    res = run(["git", "-C", str(repo), *args], timeout=timeout, shell=False)
    if check and not res.ok:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.tail(1500)}")
    return res.stdout


# ---------------------------------------------------------------------------
# Secret redaction (applied to every tool output before it reaches a model)
# ---------------------------------------------------------------------------

SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(sk|pk|rk)_(live|test)_[A-Za-z0-9]{10,}"),
    re.compile(r"(?i)postgres(?:ql)?://[^\s:@/]+:[^\s@/]+@"),
]


def redact(text: str, extra_values: list[str] | tuple[str, ...] = ()) -> str:
    for pat in SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    for value in extra_values:
        if value and len(value) >= 8:
            text = text.replace(value, "[REDACTED]")
    return text


def find_secrets(text: str) -> list[str]:
    hits = []
    for pat in SECRET_PATTERNS:
        for m in pat.finditer(text):
            hits.append(m.group(0)[:12] + "...")
    return hits
