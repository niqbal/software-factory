"""Restart console: update the factory checkout safely, then replace the process."""

import time
from types import SimpleNamespace

from asf import restart as rs
from asf.ui import Portfolio
from conftest import make_repo, sh


def clone_with_upstream(tmp_path):
    origin = make_repo(tmp_path / "origin", {"asf/__init__.py": "", "pyproject.toml": "[project]\n"})
    sh(tmp_path, "git", "clone", "-q", str(origin), "checkout")
    return origin, tmp_path / "checkout"


def head(repo):
    return sh(repo, "git", "rev-parse", "HEAD").strip()


def test_checkout_fast_forwards_to_the_latest_main(tmp_path):
    origin, checkout = clone_with_upstream(tmp_path)
    (origin / "asf" / "new.py").write_text("x = 1\n")
    sh(origin, "git", "add", "-A")
    sh(origin, "git", "commit", "-q", "-m", "new code")
    notes = rs.update_checkout(checkout, python="/bin/false")
    assert head(checkout) == head(origin)
    assert any(n.startswith("pulled main") for n in notes)
    assert rs.update_checkout(checkout) == [f"main is up to date at {head(origin)[:7]}"]


def test_a_dirty_or_diverged_checkout_is_not_pulled(tmp_path):
    origin, checkout = clone_with_upstream(tmp_path)
    (origin / "asf" / "new.py").write_text("x = 1\n")
    sh(origin, "git", "add", "-A")
    sh(origin, "git", "commit", "-q", "-m", "new code")
    (checkout / "asf" / "__init__.py").write_text("# local edit\n")
    before = head(checkout)
    assert "uncommitted changes" in rs.update_checkout(checkout)[-1]
    assert head(checkout) == before
    sh(checkout, "git", "commit", "-qam", "local commit")
    assert "diverged" in rs.update_checkout(checkout)[-1]


def test_fresh_env_keeps_asf_variables_and_falls_back_without_a_shell():
    env = rs.fresh_env({"SHELL": "/nonexistent/shell", "PATH": "/usr/bin", "ASF_HOME": "/x", "SECRET": "s"})
    assert env["ASF_HOME"] == "/x" and env["PATH"] == "/usr/bin"


def portfolio(tmp_path, monkeypatch, problem=None):
    monkeypatch.setenv("ASF_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(rs, "update_checkout", lambda root: ["pulled main: aaaaaaa -> bbbbbbb"])
    monkeypatch.setattr(rs, "preflight", lambda root: problem)
    monkeypatch.setattr(rs, "fresh_env", lambda: {"PATH": "/fresh/bin"})
    pf = Portfolio()
    pf.execs = []
    pf._exec = lambda path, argv, env: pf.execs.append((path, argv, env))
    return pf


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.02)
    return cond()


def test_restart_waits_for_running_jobs_then_replaces_the_process(tmp_path, monkeypatch):
    pf = portfolio(tmp_path, monkeypatch)
    busy = SimpleNamespace(busy={"JOB-1"})
    pf.consoles["p"] = busy
    msg = pf.restart(wait_poll=0.02)
    assert "after 1 running job" in msg and "JOB-1" in msg
    time.sleep(0.1)
    assert pf.execs == [] and pf.health()["restart"]["phase"] == "waiting"
    assert "already" in pf.restart()                # a second click does not start a second restart
    busy.busy.clear()
    assert wait_for(lambda: pf.execs)
    path, argv, env = pf.execs[0]
    assert argv[1:3] == ["-m", "asf.cli"] and env["PATH"] == "/fresh/bin"
    assert "pulled main" in env["ASF_RESTART_NOTES"] and "ASF_START_SCHEDULER" not in env


def test_restart_now_does_not_wait(tmp_path, monkeypatch):
    pf = portfolio(tmp_path, monkeypatch)
    pf.consoles["p"] = SimpleNamespace(busy={"JOB-1"})
    pf.restart(now=True, wait_poll=0.02)
    assert wait_for(lambda: pf.execs)


def test_new_code_that_does_not_load_keeps_the_console_running(tmp_path, monkeypatch):
    pf = portfolio(tmp_path, monkeypatch, problem="SyntaxError: invalid syntax")
    pf.scheduler.start()
    pf.restart(wait_poll=0.02)
    assert wait_for(lambda: (pf.restart_state or {}).get("phase") == "failed")
    assert pf.execs == [] and "SyntaxError" in pf.restart_state["message"]
    assert pf.scheduler.is_running()                # it was running before: started again
    pf.scheduler.stop()
