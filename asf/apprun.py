"""Start the project's app for browser checks, and run the e2e suite against it.

`app:` in factory.yaml says how to start the app. The factory starts it in whatever checkout
is being checked (a job's sandbox, the base commit, a post-merge worktree), waits for its health
check, hands its URL to the e2e suite as ASF_APP_URL (or to the reproducer agent), and stops
it afterwards, process group and all.

The app runs with the same scrubbed environment as every sandbox command: no credentials.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import socket
import tempfile
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from .config import Config, Package
from .util import ExecResult, run, sandbox_env


class AppError(RuntimeError):
    pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _health_url(health: str, port: int) -> str:
    if health.startswith(("http://", "https://")):
        return health.replace("{port}", str(port))
    return f"http://127.0.0.1:{port}/{health.lstrip('/')}"


@contextlib.contextmanager
def running_app(cfg: Config, root: Path, log_path: Path | None = None) -> Iterator[str]:
    """Start the app in `root`, yield its base URL once healthy, stop it on exit."""
    app = cfg.app
    if not app:
        raise AppError("no app is configured (app.start in factory.yaml)")
    port = int(app.get("port") or 0) or free_port()
    command = str(app["start"]).replace("{port}", str(port))
    env = sandbox_env({**(cfg.get("execution.env") or {}), "PORT": str(port)})
    # Never inside the checkout: the log would end up in the job's commit.
    log_path = log_path or Path(tempfile.gettempdir()) / f"asf-app-{os.getpid()}-{port}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "ab")
    proc = subprocess.Popen(command, shell=True, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True, executable="/bin/bash")
    url = f"http://127.0.0.1:{port}"
    try:
        _wait_healthy(proc, _health_url(str(app.get("health") or "/"), port),
                      float(app.get("start_timeout_s") or 60), log_path)
        yield url
    finally:
        _stop(proc)
        log.close()


def _wait_healthy(proc: subprocess.Popen, url: str, timeout: float, log_path: Path) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AppError(f"the app exited with code {proc.returncode} before it was healthy:\n"
                           f"{_tail(log_path)}")
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if 200 <= r.status < 400:
                    return
                last = f"HTTP {r.status}"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
        except (urllib.error.URLError, OSError) as e:
            last = str(e)
        time.sleep(0.25)
    raise AppError(f"the app was not healthy at {url} after {timeout:.0f}s ({last}):\n{_tail(log_path)}")


def _tail(path: Path, chars: int = 2000) -> str:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return "(no app log)"
    return text[-chars:]


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


# ---------------------------------------------------------------------------------- e2e --

def e2e_command(pkg: Package, args: str = "", url: str = "") -> str:
    template = pkg.commands["e2e"]
    cmd = template.replace("{args}", args) if "{args}" in template else (template + (" " + args if args else ""))
    return (f"ASF_APP_URL={url} " if url else "") + cmd.strip()


def run_e2e(cfg: Config, root: Path, exec_fn: Callable[[str, str], ExecResult],
            pkg: Package, args: str = "", log_path: Path | None = None) -> ExecResult:
    """Run a package's e2e command in `root` with the app running there (when one is configured).

    `exec_fn(command, cwd)` runs a command in the checkout (a sandbox's exec, or plain run)."""
    if not cfg.app:
        return exec_fn(e2e_command(pkg, args), pkg.root)
    try:
        with running_app(cfg, root, log_path) as url:
            return exec_fn(e2e_command(pkg, args, url), pkg.root)
    except AppError as e:
        return ExecResult(1, "", f"[asf] could not start the app: {e}", 0.0)


def suite_ok(res: ExecResult) -> bool:
    """A whole e2e suite run passed. An empty suite (pytest exit 5, nothing collected) is fine."""
    return res.ok or (res.code == 5 and not res.timed_out)


def run_e2e_in(cfg: Config, root: Path, pkg: Package, args: str = "", timeout: float = 900,
               log_path: Path | None = None) -> ExecResult:
    """run_e2e in a plain checkout (no sandbox object), e.g. a post-merge worktree."""
    env = sandbox_env(cfg.get("execution.env"))
    return run_e2e(cfg, root, lambda cmd, cwd: run(cmd, cwd=Path(root) / cwd, timeout=timeout, env=env),
                   pkg, args, log_path)


# ------------------------------------------------------------- pytest failure types --

@dataclass
class Failure:
    """Why an e2e run did not pass, read from pytest's output.

    kind: "pass" | "assertion" (the test's own check failed: the bug shows) | "timeout" |
    "selector" | "crash" (anything else: import error, setup error, app down) | "no_tests".
    Only "assertion" proves the bug; the other kinds prove the test is broken.
    """
    kind: str
    detail: str

    @property
    def is_assertion(self) -> bool:
        return self.kind == "assertion"


_SUMMARY = re.compile(r"^(FAILED|ERROR)\s+(\S+)(?:\s+-\s+(.*))?$", re.M)


def classify(res: ExecResult) -> Failure:
    out = res.stdout + "\n" + res.stderr
    if res.ok:
        return Failure("pass", "passed")
    if res.timed_out:
        return Failure("timeout", "the e2e command timed out")
    if res.code == 5 or re.search(r"\bno tests ran\b", out):
        return Failure("no_tests", "no tests were collected")
    kinds = []
    for m in _SUMMARY.finditer(out):
        outcome, test, msg = m.group(1), m.group(2), (m.group(3) or "").strip()
        kinds.append((_kind(outcome, msg, out), f"{test}: {msg}"[:400]))
    if not kinds:
        if "[asf] could not start the app" in out:
            return Failure("crash", out.strip()[-600:])
        return Failure("crash", f"exit {res.code} without a pytest failure summary: {out.strip()[-600:]}")
    worst = next((k for k in kinds if k[0] != "assertion"), kinds[0])
    return Failure(worst[0], "; ".join(d for _, d in kinds)[:1500])


def _kind(outcome: str, msg: str, out: str) -> str:
    if outcome == "ERROR":
        return "crash"   # setup/teardown or collection: the test never got to its check
    low = msg.lower()
    if "timeouterror" in low or "timeout" in low and "exceeded" in low:
        # Playwright waits for an element that never appears: a selector problem, not the bug.
        if re.search(r"waiting for (get_by|locator|selector)", out, re.I) or "locator" in low:
            return "selector"
        return "timeout"
    if "strict mode violation" in low or "not attached" in low:
        return "selector"
    if low.startswith("assert") or low.startswith("assertionerror") or "assertionerror" in low.split(":")[0]:
        return "assertion"
    return "crash"

