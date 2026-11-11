"""One scheduler for every project the factory works on.

Modes (scheduler.mode in ~/.asf/projects.yaml, switchable from the console):

  priority   Strict priority: run the highest-priority project's work until it has nothing
             runnable, then the next project. Lower `priority` numbers go first.
  combined   Interleave: take jobs from projects in turn (priority order breaks ties), so every
             project keeps moving.
  separate   Each project gets its own runner, as if it had its own factory. Projects do not
             wait for each other; the global concurrency cap still applies.

In every mode, up to `concurrency` jobs run at once, and within a project work that is
further along (verifying, reviewing) runs before new work. All projects share one model
subscription, so a usage-limit error pauses all model work until the stated reset time. With
`auto_resume`, the jobs it interrupted then continue where they stopped.

    scheduler: {mode: priority, concurrency: 1, auto_resume: true}
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
DEFAULTS = {"mode": "priority", "concurrency": 1, "auto_resume": True}
STAGE = {s: i for i, s in enumerate(STATES)}
INTAKE_EVERY = 60.0


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
        self.limited_until: datetime | None = None
        self.limit_reason = ""
        self.limit_jobs: list[tuple[str, str]] = []      # jobs a usage limit interrupted
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
        return {**s, "running": self.is_running(), "jobs": [{"project": p, "job": j} for p, j in self.running.values()],
                "limited_until": self.limited_until.isoformat() if self.limited_until else None,
                "limit_reason": self.limit_reason, "waiting_to_resume": len(self.limit_jobs),
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
            if c.f.paused():
                continue
            out.append((p, c))
        return out  # already in priority order

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
                if self._limited():
                    self.stop_event.wait(15)
                    continue
                self._resume_limited()
                cap = int(self.settings["concurrency"])
                free = cap - len(self.running)
                if free > 0:
                    picks = self.pick(free)
                    for p, c, job_id in picks:
                        self._launch(p, c, job_id)
                    if not picks and not self.running:
                        self.idle_reason = "nothing runnable: every job is finished or waiting on you"
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

    # ------------------------------------------------------------- usage limits --
    def _hit_limit(self, project: str, job_id: str, err: str) -> None:
        until = limits.reset_time(err)
        with self.lock:
            if not self.limited_until or until > self.limited_until:
                self.limited_until = until
            self.limit_reason = err.split(": ", 1)[-1][:200]
            self.limit_jobs.append((project, job_id))
        self._say(f"usage limit hit by {project}/{job_id}; pausing all model work until "
                  f"{until.strftime('%H:%M %Z')}")

    def _limited(self) -> bool:
        if self.limited_until and datetime.now().astimezone() < self.limited_until:
            self.idle_reason = f"usage limit: paused until {self.limited_until.strftime('%H:%M %Z')}"
            return True
        return False

    def _resume_limited(self) -> None:
        if not self.limited_until:
            return
        self.limited_until = None
        jobs, self.limit_jobs = self.limit_jobs, []
        if not self.settings.get("auto_resume", True):
            self._say(f"usage limit lifted; {len(jobs)} job(s) left for you to retry (auto_resume off)")
            return
        for project, job_id in jobs:
            try:
                c = self.portfolio.console(project)
                if c.f.store.job(job_id)["state"] == "failed":
                    c.f.retry(job_id)
                    self._say(f"usage limit lifted: {project}/{job_id} resumes")
            except Exception as e:  # noqa: BLE001
                self._say(f"could not resume {project}/{job_id}: {e}")
