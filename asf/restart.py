"""Restart the console process on the latest code, with a fresh environment.

The console runs from a checkout of the factory itself (an editable install). A restart:

1. brings that checkout up to date: a fast-forward pull of its branch when the tree is clean
   and has an upstream, and a reinstall when pyproject.toml changed;
2. checks the new code at least imports, so a broken checkout never replaces a working console;
3. reads the environment from a fresh login shell (PATH and other exports as a new terminal
   would have them), keeping ASF_* variables from the current one;
4. replaces this process with a new one started with the same arguments.

Job state lives in the store, so jobs carry on from their current step in the new process.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .util import git, run

KEEP_PREFIXES = ("ASF_",)
MARKER = "__ASF_ENV_FOLLOWS__"


def code_root() -> Path:
    """The checkout the running factory code comes from."""
    return Path(__file__).resolve().parent.parent


def update_checkout(root: Path, python: str = sys.executable) -> list[str]:
    """Fast-forward the checkout to its upstream if that is safe. Returns notes on what happened."""
    if not (root / ".git").exists():
        return [f"{root} is not a git checkout; restarting the installed code as it is"]
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
    notes = [] if branch in ("main", "master") else [f"the console runs from branch {branch}, not main"]
    before = git(root, "rev-parse", "HEAD", check=False).strip()
    if git(root, "status", "--porcelain", "--untracked-files=no", check=False).strip():
        return notes + [f"{root} has uncommitted changes: not pulling; restarting on {before[:7]}"]
    upstream = git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False).strip()
    if not upstream or "fatal" in upstream:
        return notes + [f"{branch} has no upstream: restarting on local {before[:7]}"]
    remote = upstream.split("/", 1)[0]
    if not run(["git", "-C", str(root), "fetch", "-q", remote], shell=False, timeout=120).ok:
        return notes + [f"could not fetch {remote}: restarting on local {before[:7]}"]
    res = run(["git", "-C", str(root), "merge", "--ff-only", "-q", upstream], shell=False, timeout=120)
    if not res.ok:
        return notes + [f"{branch} has diverged from {upstream}: not pulling; restarting on local {before[:7]}"]
    after = git(root, "rev-parse", "HEAD", check=False).strip()
    if after == before:
        return notes + [f"{branch} is up to date at {after[:7]}"]
    notes.append(f"pulled {branch}: {before[:7]} -> {after[:7]}")
    changed = git(root, "diff", "--name-only", before, after, check=False).split()
    if "pyproject.toml" in changed:
        inst = run([python, "-m", "pip", "install", "-q", "-e", str(root)], shell=False, timeout=600)
        notes.append("reinstalled (pyproject.toml changed)" if inst.ok
                     else f"reinstall failed: {inst.tail(300)}")
    return notes


def preflight(root: Path, python: str = sys.executable) -> str | None:
    """None when the code imports in a fresh interpreter, else the error."""
    res = run([python, "-c", "import asf.cli, asf.ui, asf.orchestrator, asf.scheduler"],
              cwd=root, shell=False, timeout=120)
    return None if res.ok else res.tail(1500)


def fresh_env(current: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a new login shell would have, plus this process's ASF_* variables.
    Falls back to the current environment when the shell cannot be asked."""
    current = dict(os.environ if current is None else current)
    shell = current.get("SHELL") or "/bin/zsh"
    base = {k: current[k] for k in ("HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "TMPDIR") if k in current}
    env: dict[str, str] = {}
    try:
        # stdin from /dev/null: an interactive shell on the console's terminal would take it over.
        # The marker skips anything the profile prints before the environment.
        p = subprocess.run([shell, "-l", "-i", "-c", f"printf '%s' '{MARKER}'; env -0"], env=base,
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        out = p.stdout.decode(errors="replace")
        if p.returncode == 0 and MARKER in out:
            for entry in out.split(MARKER, 1)[1].split("\0"):
                if "=" in entry:
                    k, v = entry.split("=", 1)
                    env[k] = v
    except (OSError, subprocess.SubprocessError):
        pass
    if "PATH" not in env:
        env = dict(current)
    # ASF_* settings, and PYTHONPATH since it decides which code the restarted console runs.
    env.update({k: v for k, v in current.items() if k.startswith(KEEP_PREFIXES) or k == "PYTHONPATH"})
    return env


def command() -> list[str]:
    """The command that started this console, run by the same interpreter."""
    return [sys.executable, "-m", "asf.cli", *sys.argv[1:]]
