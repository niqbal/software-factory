"""One scheduler for every project the factory works on.

Modes (scheduler.mode in ~/.asf/projects.yaml, switchable from the console):

  priority   Strict priority: run the highest-priority project's work until it has nothing
             runnable, then the next project. Lower `priority` numbers go first.
  combined   Interleave: take jobs from projects in turn (priority order breaks ties), so every
             project keeps moving.
  separate   Each project gets its own runner, as if it had its own factory. Projects do not
             wait for each other; the global concurrency cap still applies.

In every mode, up to `concurrency` jobs run at once, and within a project work that is
further along (verifying, reviewing) runs before new work.

Usage limits are tracked per model backend (claude, cursor, ...) and shared by all projects,
since they share subscriptions. A limit blocks only the projects that have no other usable
backend. A project on a pool of backends keeps going: with `auto_resume`, the interrupted job
is retried at once and routes to the next backend; otherwise it waits for the reset.

With `auto_retry`, a job that stopped without needing a human (a transient backend error, or an
attempt that ended without finishing; see Factory.retry_reason) is retried after a short delay,
up to `limits.auto_retries` times per job. Each retry is a `job.auto_retry` event on the job.

    scheduler: {mode: priority, concurrency: 1, auto_resume: true, auto_retry: true}
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any

import yaml

from . import limits
from .models import STATES, TERMINAL, WAITING
from .projects import asf_home

MODES = ("priority", "combined", "separate")
DEFAULTS = {"mode": "priority", "concurrency": 1, "auto_resume": True, "auto_retry": True}
STAGE = {s: i for i, s in enumerate(STATES)}
INTAKE_EVERY = 60.0
RETRY_AFTER = 60.0   # seconds a stopped job stays visible before an automatic retry


def load_settings() -> dict[str, Any]:
    p = asf_home() / "projects.yaml"
    data = (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}
    return {**DEFAULTS, **(data.get("scheduler") or {})}


def save_settings(**fields: Any) -> dict[str, Any]:
    p = asf_home() / "projects.yaml"
    data = (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}
    current = {**DEFAULTS, **(data.get("scheduler") or {})}
    if "mode" in fields and fields["mode"] not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    if "concurrency" in fields:
        fields["concurrency"] = max(1, min(8, int(fields["concurrency"])))
    current.update({k: v for k, v in fields.items() if k in DEFAULTS})
    data["scheduler"] = current
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data, sort_keys=False))
    return current


class Scheduler:
    def __init__(self, portfolio: Any):
        self.portfolio = portfolio
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.running: dict[str, tuple[str, str]] = {}   # slot -> (project, job)
        self.limit_jobs: list[tuple[str, str]] = []      # jobs waiting for a backend to free up
        self.last_intake: dict[str, float] = {}
        self.rr = 0                                      # round-robin cursor (combined mode)
        self.idle_reason = ""
        self.log: list[str] = []

    # ------------------------------------------------------------------ control --
    @property
    def settings(self) -> dict[str, Any]:
        return load_settings()

    def is_running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self) -> str:
        with self.lock:
            if self.is_running():
                return "already running"
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
        return f"scheduler started ({self.settings['mode']} mode)"

    def stop(self) -> str:
        self.stop_event.set()
        return "stopping: running jobs finish their current step"

    def _say(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} scheduler: {msg}"
        self.log = (self.log + [line])[-200:]

    def status(self) -> dict[str, Any]:
        s = self.settings
        lim = limits.all_limits()
        known = set(lim)
        for p in self.portfolio.projects():
            try:
                known |= set(self.backends_of(self.portfolio.console(p.key)))
            except Exception:  # noqa: BLE001
                pass
        backends = {b: (lim[b]["until"] if b in lim else None) for b in sorted(known)}
        return {**s, "backends": backends, "running": self.is_running(), "jobs": [{"project": p, "job": j} for p, j in self.running.values()],
                "limits": {b: e["until"] for b, e in lim.items()},
                "limited_until": min((e["until"] for e in lim.values()), default=None),
                "waiting_to_resume": len(self.limit_jobs),
                "idle_reason": self.idle_reason, "log": self.log[-30:], "modes": MODES}

    # ---------------------------------------------------------------- selection --
    def _projects(self) -> list[Any]:
        out = []
        for p in self.portfolio.projects():
            if p.archived or not p.available:
                continue
            try:
                c = self.portfolio.console(p.key)
            except Exception:  # noqa: BLE001
                continue
            if c.f.paused() or not self.usable(c):
                continue
            out.append((p, c))
        return out  # already in priority order

    @staticmethod
    def backends_of(c: Any) -> list[str]:
        """The model backends a project can use, from its models config."""
        m = c.f.cfg.get("models") or {}
        kind = m.get("provider", "anthropic")
        if kind == "pool":
            return list((m.get("backends") or {}).keys())
        return [{"claude_code": "claude", "cursor_cli": "cursor"}.get(kind, kind)]

    def usable(self, c: Any) -> bool:
        return any(limits.limited_until(b) is None for b in self.backends_of(c))

    def _runnable(self, c: Any) -> list[dict[str, Any]]:
        jobs = [j for j in c.f.store.jobs()
                if j["state"] not in TERMINAL and j["state"] not in WAITING and j["state"] != "paused"
                and j["id"] not in c.busy]
        # Finish what is furthest along first, then oldest first.
        jobs.sort(key=lambda j: (-STAGE.get(j["state"], 0), j["created"]))
        return jobs

    def _intake(self, key: str, c: Any) -> None:
        if time.time() - self.last_intake.get(key, 0) < INTAKE_EVERY:
            return
        self.last_intake[key] = time.time()
        try:
            new = c.f.poll()
            if new:
                self._say(f"{key}: {len(new)} new job(s)")
        except Exception as e:  # noqa: BLE001
            self._say(f"{key}: intake failed: {e}")

    def pick(self, slots_free: int) -> list[tuple[Any, Any, str]]:
        """Choose up to slots_free (project, console, job_id) to start, per the mode."""
        mode = self.settings["mode"]
        projects = self._projects()
        for p, c in projects:
            self._intake(p.key, c)
        busy_projects = {proj for proj, _ in self.running.values()}
        queues = [(p, c, self._runnable(c)) for p, c in projects]
        queues = [q for q in queues if q[2]]
        picks: list[tuple[Any, Any, str]] = []
        if mode == "priority":
            for p, c, jobs in queues:
                for j in jobs:
                    if len(picks) < slots_free:
                        picks.append((p, c, j["id"]))
                if picks or p.key in busy_projects:
                    break  # strict: lower-priority projects wait while this one has work
        elif mode == "combined":
            if queues:
                order = queues[self.rr % len(queues):] + queues[:self.rr % len(queues)]
                self.rr += 1
                idx = 0
                while len(picks) < slots_free and any(q[2] for q in order):
                    p, c, jobs = order[idx % len(order)]
                    if jobs:
                        picks.append((p, c, jobs.pop(0)["id"]))
                    idx += 1
        else:  # separate: one runner per project, each project independent
            for p, c, jobs in queues:
                if len(picks) >= slots_free:
                    break
                if p.key not in busy_projects:
                    picks.append((p, c, jobs[0]["id"]))
        return picks

    # --------------------------------------------------------------------- loop --
    def _loop(self) -> None:
        self._say(f"started in {self.settings['mode']} mode, concurrency {self.settings['concurrency']}")
        try:
            while not self.stop_event.is_set():
                self._resume_limited()
                self._retry_stopped()
                cap = int(self.settings["concurrency"])
                free = cap - len(self.running)
                if free > 0:
                    picks = self.pick(free)
                    for p, c, job_id in picks:
                        self._launch(p, c, job_id)
                    if not picks and not self.running:
                        lim = limits.all_limits()
                        self.idle_reason = ("waiting for usage limits: " + ", ".join(
                            f"{b} until {e['until'][11:16]}" for b, e in lim.items())) if lim and self.limit_jobs \
                            else "nothing runnable: every job is finished or waiting on you"
                    elif picks:
                        self.idle_reason = ""
                self.stop_event.wait(2)
        finally:
            self._say("stopped")

    def _launch(self, p: Any, c: Any, job_id: str) -> None:
        slot = f"{p.key}:{job_id}"
        with self.lock:
            self.running[slot] = (p.key, job_id)
        self._say(f"{p.key}: running {job_id}")

        def go() -> None:
            try:
                c.current = job_id
                final = c.run_guarded(job_id)
                job = c.f.store.job(job_id)
                err = job["data"].get("error", "") if final == "failed" else ""
                if final == "failed" and limits.is_limit_error(err):
                    self._hit_limit(p.key, job_id, err)
                elif final:
                    self._say(f"{p.key}: {job_id} -> {final}")
            except Exception as e:  # noqa: BLE001
                self._say(f"{p.key}: {job_id} crashed: {e}")
            finally:
                c.current = None
                with self.lock:
                    self.running.pop(slot, None)
        threading.Thread(target=go, daemon=True).start()

    # ---------------------------------------------------------- automatic retry --
    def _retry_stopped(self, now: float | None = None) -> None:
        """Retry jobs that stopped without needing a human, once they have sat RETRY_AFTER seconds.

        Reads the stored jobs each time, so jobs stopped by a console run, `asf run` or before a
        restart are covered too. The retried job becomes runnable and is picked like any other."""
        if not self.settings.get("auto_retry", True):
            return
        now = now or time.time()
        for p, c in self._projects():
            try:
                for j in c.f.store.jobs(["failed", "needs_human"]):
                    if j["id"] in c.busy or now - (j.get("updated") or 0) < RETRY_AFTER:
                        continue
                    if not c.f.retry_reason(j) or j["data"].get("auto_retry_exhausted"):
                        continue
                    state = c.f.auto_retry(j["id"])
                    if state:
                        n = c.f.store.job(j["id"])["data"].get("auto_retries", 0)
                        self._say(f"{p.key}: {j['id']} retried automatically (#{n}) from {state}")
                    else:
                        self._say(f"{p.key}: {j['id']} stopped again; automatic retries used up")
            except Exception as e:  # noqa: BLE001 - one project's trouble must not stop the loop
                self._say(f"{p.key}: automatic retry failed: {e}")

    # ------------------------------------------------------------- usage limits --
    def _hit_limit(self, project: str, job_id: str, err: str) -> None:
        backend = limits.backend_of_error(err)
        until = limits.reset_time(err)
        limits.record_limit(backend, until, err.split(": ", 1)[-1])
        with self.lock:
            self.limit_jobs.append((project, job_id))
        self._say(f"{backend} usage limit hit by {project}/{job_id}; {backend} paused until "
                  f"{until.strftime('%H:%M %Z')}")
        self._resume_limited()  # another backend may take the job right away

    def _limited(self) -> bool:
        """True when no backend at all is usable (every project is blocked)."""
        lim = limits.all_limits()
        return bool(lim) and not any(self.usable(c) for _, c in
                                     [(p, self.portfolio.console(p.key)) for p in self.portfolio.projects()
                                      if not p.archived and p.available])

    def _resume_limited(self) -> None:
        if not self.limit_jobs:
            return
        auto = self.settings.get("auto_resume", True)
        keep = []
        for project, job_id in self.limit_jobs:
            try:
                c = self.portfolio.console(project)
            except Exception:  # noqa: BLE001
                continue
            if not self.usable(c):
                keep.append((project, job_id))  # its backends are all limited: wait
                continue
            if not auto:
                self._say(f"{project}/{job_id} can run again; retry it (auto_resume is off)")
                continue
            try:
                if c.f.store.job(job_id)["state"] == "failed":
                    c.f.retry(job_id)
                    self._say(f"{project}/{job_id} resumes on an available backend")
            except Exception as e:  # noqa: BLE001
                self._say(f"could not resume {project}/{job_id}: {e}")
        self.limit_jobs = keep
