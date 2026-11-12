"""`asf ui`: a local web console for the factory.

Serves one page and a small JSON API on 127.0.0.1. Everything the CLI can do for an owner is a
button: run the queue, review and merge, reject, revise, resume, retry, queue backlog items.
Mutating requests need the per-process token embedded in the page, so other sites cannot drive
it through the browser.
"""

from __future__ import annotations

import collections
import json
import secrets as pysecrets
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from . import metrics
from .models import TERMINAL, WAITING
from .orchestrator import Factory
from .util import git, run

RUNNABLE_SKIP = TERMINAL | WAITING | {"paused"}


class Console:
    """State shared by request handlers: the factory, a worker thread, and a live log."""

    def __init__(self, factory: Factory):
        self.f = factory
        self.log: collections.deque[str] = collections.deque(maxlen=400)
        self.activity: collections.deque[dict[str, Any]] = collections.deque(maxlen=50)
        self.lock = threading.Lock()
        self.worker: threading.Thread | None = None
        self.stop = threading.Event()
        self.stop_all = threading.Event()
        self.current: str | None = None
        self.token = pysecrets.token_urlsafe(24)
        self.busy: set[str] = set()  # jobs being run right now, by any thread
        factory.echo = self._echo

    def run_guarded(self, job_id: str) -> str | None:
        """run_job, unless another thread is already running this job."""
        with self.lock:
            if job_id in self.busy:
                return None
            self.busy.add(job_id)
        try:
            return self.f.run_job(job_id)
        finally:
            with self.lock:
                self.busy.discard(job_id)

    def job_running(self, job_id: str) -> bool:
        with self.lock:
            return job_id == self.current or job_id in self.busy

    def start_reconciler(self, interval: float = 20.0) -> None:
        """Notice merges done outside the factory and verify them, without anyone clicking."""
        def loop() -> None:
            while not self.stop_all.is_set():
                for job in self.f.store.jobs(["awaiting_review", "merged", "observing", "deploying"]):
                    try:
                        if job["state"] == "awaiting_review" and not self.f._review_settled(job):
                            continue
                        self._echo(f"reconcile: {job['id']} merged; verifying")
                        self.run_guarded(job["id"])
                    except Exception as e:  # noqa: BLE001
                        self._echo(f"reconcile {job['id']}: {e}")
                self.stop_all.wait(interval)
        threading.Thread(target=loop, daemon=True).start()

    # ---------------------------------------------------------------- log/activity --
    def _echo(self, msg: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {msg}")

    def _record(self, action: str, ok: bool, message: str) -> None:
        self.activity.appendleft({"at": time.time(), "action": action, "ok": ok, "message": message})
        self._echo(f"{'ok' if ok else 'ERROR'} {action}: {message}")

    def background(self, action: str, fn: Any) -> None:
        def go() -> None:
            try:
                result = fn()
                self._record(action, True, str(result) if result is not None else "done")
            except Exception as e:  # noqa: BLE001
                self._record(action, False, f"{e}")
                self.log.append(traceback.format_exc()[-1500:])
        threading.Thread(target=go, daemon=True).start()

    # ---------------------------------------------------------------------- worker --
    def _sched_running(self) -> bool:
        sch = getattr(self, "scheduler", None)
        return sch.is_running() if sch else self.worker_running()

    def worker_running(self) -> bool:
        return bool(self.worker and self.worker.is_alive())

    def start_worker(self) -> str:
        with self.lock:
            if self.worker_running():
                return "already running"
            self.stop.clear()
            self.worker = threading.Thread(target=self._work, daemon=True)
            self.worker.start()
        return "started"

    def _work(self) -> None:
        self._echo("worker: started")
        try:
            while not self.stop.is_set():
                try:
                    self.f.poll()
                except Exception as e:  # noqa: BLE001
                    self._echo(f"intake error: {e}")
                progressed = False
                for job in self.f.store.jobs():
                    if self.stop.is_set() or self.f.paused():
                        break
                    if job["state"] in RUNNABLE_SKIP and job["state"] not in WAITING:
                        continue
                    before = job["state"]
                    self.current = job["id"]
                    after = self.run_guarded(job["id"])
                    self.current = None
                    progressed = progressed or (after is not None and after != before)
                if not progressed:
                    break  # everything left is waiting on a person or finished
        finally:
            self.current = None
            self._echo("worker: idle")

    # ----------------------------------------------------------------------- views --
    def state(self) -> dict[str, Any]:
        f, st = self.f, self.f.store
        m = metrics.compute(st)
        jobs = []
        titles = self._titles()
        for j in reversed(st.jobs()):
            d = j["data"]
            ext = titles.get(j["work_item_id"], ("?", ""))
            jobs.append({
                "id": j["id"], "item": ext[0], "title": ext[1], "state": j["state"],
                "bucket": metrics.bucket_of(j["state"]), "tier": j["tier"],
                "cost": m["per_job"].get(j["id"], {}).get("cost_usd", 0),
                "stops": m["per_job"].get(j["id"], {}).get("stops", 0),
                "reason": current_reason(j["state"], d),
                "branch": d.get("branch"), "owner_review": bool(d.get("owner_review")),
                "created": j["created"], "updated": j["updated"], "running": self.job_running(j["id"]),
            })
        return {"repo": f.repo.name, "default_branch": f.cfg.default_branch, "metrics": m, "jobs": jobs,
                "backlog": self.backlog(),
                "worker": {"running": self._sched_running(), "current": self.current,
                           "scheduler": getattr(self, "scheduler", None) is not None},
                "paused": f.paused() or "", "log": list(self.log)[-80:], "activity": list(self.activity)[:12],
                "shadow": bool(f.cfg.get("autonomy.shadow")), "max_merge_tier": f.cfg.get("autonomy.max_merge_tier")}

    def _titles(self) -> dict[str, tuple[str, str]]:
        out = {}
        for r in self.f.store.db.execute("SELECT id, external_id, payload FROM work_items"):
            try:
                title = json.loads(r["payload"]).get("title", "")
            except Exception:  # noqa: BLE001
                title = ""
            out[r["id"]] = (r["external_id"], title)
        return out

    def _local_dir(self, key: str, default: str | None = None) -> Path | None:
        rel = self.f.cfg.get(f"intake.local.{key}") or default
        if not rel:
            return None
        p = Path(rel)
        return p if p.is_absolute() else self.f.repo / p

    def backlog(self) -> list[dict[str, Any]]:
        """Backlog files (intake.local.backlog) with their dependencies resolved."""
        folder = self._local_dir("backlog")
        if not folder or not folder.exists():
            return []
        shipped = {self._titles().get(j["work_item_id"], ("",))[0] for j in self.f.store.jobs(["done"])}
        done_dir = self._local_dir("done")
        if done_dir and done_dir.exists():
            shipped |= {p.stem for p in done_dir.glob("*.md")}
        out = []
        for p in sorted(folder.glob("*.md")):
            meta = _front_matter(p)
            deps = [str(x) for x in meta.get("depends_on", []) or []]
            missing = [x for x in deps if x not in shipped]
            out.append({"id": p.stem, "title": meta.get("title", p.stem), "depends_on": deps,
                        "blocked_by": missing, "ready": not missing})
        return out

    def job_detail(self, job_id: str) -> dict[str, Any]:
        st = self.f.store
        j = st.job(job_id)
        d = j["data"]
        item = self._titles().get(j["work_item_id"], ("?", ""))
        report = ""
        if d.get("report") and Path(d["report"]).exists():
            report = Path(d["report"]).read_text()
        by_role: dict[str, float] = {}
        backend_by_role: dict[str, str] = {}
        for r in st.runs(job_id):
            by_role[r["role"]] = round(by_role.get(r["role"], 0) + r["cost_usd"], 2)
            model = r.get("model") or ""
            backend_by_role[r["role"]] = model.split(":", 1)[0] if ":" in model else \
                ("claude" if model.startswith("claude") else model)
        events = [{"at": e["ts"], "type": e["type"],
                   "actor": e["actor"], "summary": _summarize(e)} for e in st.events(job_id)][-120:]
        keep = ("needs_human_reason", "paused_reason", "error", "feedback", "manual_verification", "gates",
                "reviews", "owner_review", "delivery", "post_merge", "deploy", "post_deploy", "acceptance",
                "acceptance_met", "implementer", "tester", "branch", "delivered_at", "merged_at", "done_at",
                "merged_by", "outcome", "triage")
        return {"id": job_id, "item": item[0], "title": item[1], "state": j["state"], "tier": j["tier"],
                "created": j["created"], "updated": j["updated"], "running": self.job_running(job_id),
                "data": {k: d.get(k) for k in keep}, "report": report, "cost_by_role": by_role,
                "backend_by_role": backend_by_role, "events": events,
                "diff": self.diff(j), "actions": self.actions(j)}

    def diff(self, j: dict[str, Any]) -> dict[str, Any]:
        d = j["data"]
        branch, repo, base = d.get("branch"), self.f.repo, self.f.cfg.default_branch
        sandbox = (d.get("sandbox") or {})
        try:
            if branch and git(repo, "rev-parse", "-q", "--verify", f"refs/heads/{branch}", check=False).strip():
                rng = [f"{base}...{branch}"]
                where = repo
            elif sandbox.get("root") and Path(sandbox["root"]).exists():
                rng, where = [sandbox["base_sha"]], Path(sandbox["root"])
            else:
                return {"stat": "", "patch": "", "where": "none"}
            stat = git(where, "diff", "--stat", *rng)
            patch = git(where, "diff", *rng)
            return {"stat": stat, "patch": patch[:200000], "truncated": len(patch) > 200000,
                    "where": "branch" if where == repo else "sandbox"}
        except Exception as e:  # noqa: BLE001
            return {"stat": f"(diff unavailable: {e})", "patch": "", "where": "error"}

    def actions(self, j: dict[str, Any]) -> list[str]:
        s = j["state"]
        acts = []
        if s == "awaiting_review":
            acts += ["merge", "revise", "reject"]
        if s == "awaiting_approval":
            acts += ["approve", "reject"]
        if s == "awaiting_deploy":
            acts += ["deploy"]
        if s == "paused":
            acts += ["resume", "reject"]
        if s in ("needs_human", "failed"):
            acts += ["retry", "reject"]
        if s not in TERMINAL and s not in WAITING and s != "paused" and not self.job_running(j["id"]):
            acts += ["run"]
        if s == "rejected" or s == "needs_human" or s == "failed":
            acts += ["rerun"]
        return acts

    # --------------------------------------------------------------------- actions --
    def act(self, job_id: str, action: str, body: dict[str, Any]) -> str:
        f = self.f
        by = body.get("by") or "owner (ui)"
        if action == "merge":
            self.background(f"merge {job_id}", lambda: f"{job_id} -> {f.merge_reviewed(job_id, by=by)}")
            return "merging; post-merge checks run next"
        if action == "deploy":
            ids = f.approve_deploy(job_id, by=by)

            def deploy_all() -> str:
                return ", ".join(f"{i} -> {self.run_guarded(i)}" for i in ids)
            self.background(f"deploy {', '.join(ids)}", deploy_all)
            return f"deploying {len(ids)} job(s): {', '.join(ids)}"
        if action == "approve":
            f.store.approve(job_id, by, "approve", body.get("reason", ""))
            self.background(f"run {job_id}", lambda: self.run_guarded(job_id))
            return "approved"
        if action == "reject":
            f.reject_job(job_id, by=by, reason=body.get("reason", ""))
            return "rejected"
        if action == "revise":
            notes = [n for n in (body.get("notes") or "").splitlines() if n.strip()]
            f.revise(job_id, notes)
            self.background(f"revise {job_id}", lambda: f.run_job(job_id))
            return "revision started"
        if action == "resume":
            f.resume(job_id, float(body.get("extra_usd", 0)), float(body.get("extra_minutes", 600)))
            self.background(f"run {job_id}", lambda: self.run_guarded(job_id))
            return "resumed"
        if action == "retry":
            state = f.retry(job_id)
            f.store.update_job(job_id, extra_minutes=f.store.job(job_id)["data"].get("extra_minutes", 0) + 600)
            self.background(f"run {job_id}", lambda: self.run_guarded(job_id))
            return f"retrying from {state}"
        if action == "run":
            if self.job_running(job_id):
                raise RuntimeError("job is already running")
            self.background(f"run {job_id}", lambda: self.run_guarded(job_id))
            return "running"
        if action == "rerun":
            ext = self._titles().get(f.store.job(job_id)["work_item_id"], ("",))[0]
            if f.store.job(job_id)["state"] not in ("rejected",):
                f.reject_job(job_id, by=by, reason="rerun from current default branch")
            new = f.poll(ids=[ext], rerun=True)
            return f"rerun queued: {', '.join(new) or 'no new job (is the item still in the ready folder?)'}"
        raise ValueError(f"unknown action {action}")

    def queue_item(self, item_id: str) -> str:
        src_dir, dst_dir = self._local_dir("backlog"), self._local_dir("path")
        if not src_dir or not dst_dir:
            raise ValueError("intake.local.backlog and intake.local.path must be configured")
        src, dst = src_dir / f"{item_id}.md", dst_dir / f"{item_id}.md"
        if not src.exists():
            raise ValueError(f"{item_id} is not in the backlog")
        dst_dir.mkdir(parents=True, exist_ok=True)
        repo = self.f.repo
        tracked = run(["git", "-C", str(repo), "ls-files", "--error-unmatch", str(src)], shell=False).ok
        if tracked:
            git(repo, "mv", str(src), str(dst))
            git(repo, "commit", "-q", "-m", f"Queue {item_id}", "--", str(src), str(dst))
        else:
            src.rename(dst)
        jobs = self.f.poll()
        return f"queued {item_id} ({len(jobs)} new job(s))"


def current_reason(state: str, d: dict[str, Any]) -> str:
    """The reason that explains the job's current state; stale reasons from earlier stops are hidden."""
    if state == "needs_human":
        return d.get("needs_human_reason") or ""
    if state == "paused":
        return d.get("paused_reason") or ""
    if state == "failed":
        return d.get("error") or ""
    if state == "rejected":
        return d.get("outcome") or d.get("needs_human_reason") or ""
    if state == "done":
        return d.get("outcome") or ""
    if state == "awaiting_deploy":
        return "ready to deploy: " + ", ".join((d.get("deploy") or {}).get("steps") or [])
    return ""


def _front_matter(p: Path) -> dict[str, Any]:
    text = p.read_text()
    if not text.startswith("---"):
        return {}
    try:
        return yaml.safe_load(text.split("---", 2)[1]) or {}
    except Exception:  # noqa: BLE001
        return {}


def _summarize(e: Any) -> str:
    p = e["payload"] if isinstance(e["payload"], dict) else {}
    t = e["type"]
    if t == "job.state":
        return f"{p.get('from')} → {p.get('to')}"
    if t == "tool.call":
        args = p.get("args") if isinstance(p.get("args"), dict) else {}
        target = args.get("path") or args.get("kind") or args.get("command") or (p.get("args") if isinstance(p.get("args"), str) else "")
        return f"{e['actor']}: {p.get('tool', '')} {target} {'(denied)' if p.get('decision') == 'deny' else ''}".strip()
    if t.startswith("notify."):
        return p.get("message", "")[:300]
    return json.dumps(p)[:300]


class Portfolio:
    """All projects under $ASF_HOME: one Console per project, created on first use."""

    def __init__(self, secrets: Any = None):
        self.secrets = secrets
        self.consoles: dict[str, Console] = {}
        self.errors: dict[str, str] = {}
        self.lock = threading.Lock()
        self.token = pysecrets.token_urlsafe(24)
        self.stop_all = threading.Event()
        from .scheduler import Scheduler
        self.scheduler = Scheduler(self)

    def projects(self) -> list[Any]:
        from .projects import discover
        return [p for p in discover() if not p.hidden]

    @staticmethod
    def _config_stamp(repo: Any, state_dir: Any) -> float:
        """mtime of the factory.yaml a project resolves to (the repo copy wins over the pending one)."""
        for cand in (Path(repo) / "factory.yaml", Path(state_dir) / "factory.yaml"):
            if cand.exists():
                return cand.stat().st_mtime
        return 0.0

    def console(self, key: str) -> Console:
        with self.lock:
            c = self.consoles.get(key)
            if c is not None:
                # Pick up an edited factory.yaml, but never swap a console out from under a job.
                stamp = getattr(c, "cfg_stamp", None)
                stale = stamp is not None and self._config_stamp(c.f.repo, c.f.state_dir) != stamp
                if not stale or c.busy or c.current:
                    return c
                del self.consoles[key]
        proj = next((p for p in self.projects() if p.key == key), None)
        if proj is None:
            raise KeyError(key)
        if not proj.available:
            raise ValueError(f"repo for {key} not found; set it in ~/.asf/projects.yaml")
        from . import config as config_mod
        from .store import Store
        cfg = config_mod.resolve(proj.repo, None, proj.state_dir)
        f = Factory(proj.repo, cfg, proj.state_dir, Store(proj.state_dir / "factory.db"),
                    secrets=self.secrets or (lambda n: None), echo=lambda m: None)
        c = Console(f)
        c.cfg_stamp = self._config_stamp(proj.repo, proj.state_dir)
        c.token = self.token
        c.project = proj
        c.scheduler = self.scheduler
        migrated = f.migrate_legacy()
        if migrated:
            c._echo(f"migrated {migrated} job(s) saved when 'done' meant delivered")
        with self.lock:
            self.consoles[key] = c
        return c

    def summary(self) -> dict[str, Any]:
        now = time.time()
        cards, inbox = [], []
        for p in self.projects():
            card: dict[str, Any] = {"key": p.key, "name": p.name, "repo": str(p.repo or ""), "priority": p.priority,
                                    "archived": p.archived, "available": p.available}
            try:
                c = self.console(p.key)
            except Exception as e:  # noqa: BLE001
                card["error"] = str(e)
                cards.append(card)
                continue
            st = c.f.store
            jobs = st.jobs()
            m = metrics.compute(st)
            titles = c._titles()
            waiting = [j for j in jobs if j["state"] in NEEDS_YOU]
            for j in waiting:
                ext = titles.get(j["work_item_id"], ("?", ""))
                since = j["data"].get("delivered_at") if j["state"] == "awaiting_review" else j["updated"]
                inbox.append({"project": p.key, "project_name": p.name, "id": j["id"], "item": ext[0], "title": ext[1],
                              "state": j["state"], "reason": current_reason(j["state"], j["data"]),
                              "owner_review": bool(j["data"].get("owner_review")), "waiting_since": since or j["updated"],
                              "cost": m["per_job"].get(j["id"], {}).get("cost_usd", 0)})
            last = max([j["updated"] for j in jobs] or [0])
            card.update({
                "needs_you": len(waiting),
                "oldest_wait_s": max([now - (j["data"].get("delivered_at") or j["updated"]) for j in waiting] or [0]),
                "in_progress": m["counts"]["in_progress"] + m["counts"]["verifying"],
                "running": c.current or next((j["id"] for j in jobs if j["id"] in c.busy), None),
                "worker": any(proj == p.key for proj, _ in self.scheduler.running.values()),
                "shipped": m["counts"]["shipped"],
                "shipped_7d": sum(1 for j in jobs if j["state"] == "done" and (j["data"].get("done_at") or 0) > now - 7 * 86400),
                "cost_per_shipped": m["cost_per_shipped_usd"], "waste_share": m["waste_share"],
                "spend": m["spend_usd"]["total"], "paused": c.f.paused() or "", "last_activity": last,
                "shadow": bool(c.f.cfg.get("autonomy.shadow")), "max_merge_tier": c.f.cfg.get("autonomy.max_merge_tier"),
            })
            cards.append(card)
        inbox.sort(key=lambda x: x["waiting_since"])
        totals = {k: sum(c.get(k) or 0 for c in cards) for k in ("needs_you", "in_progress", "shipped", "shipped_7d", "spend")}
        return {"projects": cards, "inbox": inbox, "totals": totals, "scheduler": self.scheduler.status()}

    def start_reconciler(self, interval: float = 20.0) -> None:
        def loop() -> None:
            while not self.stop_all.is_set():
                for p in self.projects():
                    if p.archived or not p.available:
                        continue
                    try:
                        c = self.console(p.key)
                    except Exception:  # noqa: BLE001
                        continue
                    for job in c.f.store.jobs(["awaiting_review", "merged", "observing", "deploying"]):
                        try:
                            if job["state"] == "awaiting_review" and not c.f._review_settled(job):
                                continue
                            c._echo(f"reconcile: {job['id']} merged; verifying")
                            c.run_guarded(job["id"])
                        except Exception as e:  # noqa: BLE001
                            c._echo(f"reconcile {job['id']}: {e}")
                self.stop_all.wait(interval)
        threading.Thread(target=loop, daemon=True).start()


NEEDS_YOU = {"awaiting_review", "awaiting_approval", "awaiting_deploy", "needs_human", "failed", "paused"}


def make_handler(portfolio: Portfolio):
    page = (files("asf") / "ui_static" / "index.html").read_text()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _send(self, code: int, body: Any, ctype: str = "application/json") -> None:
            data = body.encode() if isinstance(body, str) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            parts = path.strip("/").split("/")
            try:
                if path == "/":
                    return self._send(200, page.replace("__ASF_TOKEN__", portfolio.token), "text/html; charset=utf-8")
                if path == "/api/portfolio":
                    return self._send(200, portfolio.summary())
                if path == "/api/scheduler":
                    return self._send(200, portfolio.scheduler.status())
                if len(parts) >= 4 and parts[:2] == ["api", "p"]:
                    c = portfolio.console(parts[2])
                    if parts[3] == "state":
                        return self._send(200, c.state())
                    if parts[3] == "jobs" and len(parts) == 5:
                        return self._send(200, c.job_detail(parts[4]))
            except KeyError as e:
                return self._send(404, {"error": f"not found: {e}"})
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": str(e)})
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.headers.get("X-ASF-Token") != portfolio.token:
                return self._send(403, {"error": "missing or wrong token"})
            body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            payload = json.loads(body or b"{}")
            parts = self.path.strip("/").split("/")
            try:
                if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "settings":
                    from .projects import set_entry
                    allowed = {k: payload[k] for k in ("name", "priority", "archived", "hidden") if k in payload}
                    set_entry(parts[2], **allowed)
                    return self._send(200, {"message": "saved"})
                if parts == ["api", "scheduler", "start"]:
                    return self._send(200, {"message": portfolio.scheduler.start()})
                if parts == ["api", "scheduler", "stop"]:
                    return self._send(200, {"message": portfolio.scheduler.stop()})
                if parts == ["api", "scheduler", "settings"]:
                    from .scheduler import save_settings
                    saved = save_settings(**{k: payload[k] for k in ("mode", "concurrency", "auto_resume") if k in payload})
                    return self._send(200, {"message": f"scheduler: {saved['mode']} mode, concurrency {saved['concurrency']}"})
                if len(parts) < 4 or parts[:2] != ["api", "p"]:
                    return self._send(404, {"error": "not found"})
                c, rest = portfolio.console(parts[2]), parts[3:]
                if rest == ["worker", "start"]:
                    return self._send(200, {"message": portfolio.scheduler.start()})
                if rest == ["worker", "stop"]:
                    return self._send(200, {"message": portfolio.scheduler.stop()})
                if rest == ["intake"]:
                    return self._send(200, {"message": f"{len(c.f.poll())} new job(s)"})
                if rest == ["pause-all"]:
                    c.f.store.set_flag("paused_all", payload.get("reason") or "paused from the console")
                    return self._send(200, {"message": "paused"})
                if rest == ["resume-all"]:
                    c.f.store.set_flag("paused_all", None)
                    return self._send(200, {"message": "resumed"})
                if len(rest) == 3 and rest[0] == "backlog" and rest[2] == "queue":
                    return self._send(200, {"message": c.queue_item(rest[1])})
                if len(rest) == 3 and rest[0] == "jobs":
                    return self._send(200, {"message": c.act(rest[1], rest[2], payload)})
            except (ValueError, RuntimeError, KeyError) as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": str(e)})
            self._send(404, {"error": "not found"})

    return Handler


def serve(port: int, open_project: str | None = None, open_browser: bool = False, secrets: Any = None) -> None:
    portfolio = Portfolio(secrets)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(portfolio))
    portfolio.start_reconciler()
    url = f"http://127.0.0.1:{port}/" + (f"#p/{open_project}" if open_project else "")
    print(f"factory console: {url}  (all projects under ~/.asf; Ctrl-C to stop)", flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        portfolio.stop_all.set()
        portfolio.scheduler.stop()
        for c in portfolio.consoles.values():
            c.stop.set()
            c.stop_all.set()
