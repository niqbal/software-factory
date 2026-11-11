"""asf: command-line entry point for the software factory."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from . import config as config_mod
from . import setup_agent
from .adapters import base as adapters
from .models import WorkItem
from .orchestrator import Factory
from .store import Store
from .util import iso, sha


def load_env_file(path: str | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path:
        return values
    for line in Path(path).expanduser().read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip().removeprefix("export ").strip()] = value.strip().strip('"').strip("'")
    return values


def state_dir_for(repo: Path, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    home = Path(os.environ.get("ASF_HOME", "~/.asf")).expanduser()
    return home / f"{repo.name}-{sha(str(repo), 6)}"


def apply_overrides(cfg: config_mod.Config, sets: list[str]) -> config_mod.Config:
    raw = cfg.raw
    for item in sets or []:
        key, _, value = item.partition("=")
        node = raw
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = yaml.safe_load(value)
    errors = config_mod.validate(raw)
    if errors:
        raise config_mod.ConfigError("; ".join(errors))
    return config_mod.Config(raw=raw, source=cfg.source + " (+overrides)")


class App:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.repo = Path(args.repo).expanduser().resolve()
        self.state_dir = state_dir_for(self.repo, args.state)
        self.env = load_env_file(args.env_file)
        self._cfg: config_mod.Config | None = None
        self._factory: Factory | None = None

    def secret(self, name: str) -> str | None:
        return self.env.get(name) or os.environ.get(name)

    @property
    def cfg(self) -> config_mod.Config:
        if self._cfg is None:
            cfg = config_mod.resolve(self.repo, self.args.config, self.state_dir)
            self._cfg = apply_overrides(cfg, self.args.set)
        return self._cfg

    @property
    def store(self) -> Store:
        return self.factory.store

    def register(self, name: str | None = None, priority: int | None = None) -> None:
        """Make the project visible to `asf ui` and the scheduler before any job exists."""
        from .projects import record, set_entry
        Store(self.state_dir / "factory.db")
        record(self.state_dir, self.repo)
        entry = {k: v for k, v in (("name", name), ("priority", priority)) if v is not None}
        if entry:
            set_entry(self.state_dir.name, **entry)

    @property
    def factory(self) -> Factory:
        if self._factory is None:
            store = Store(self.state_dir / "factory.db")
            from .projects import record
            record(self.state_dir, self.repo)
            self._factory = Factory(self.repo, self.cfg, self.state_dir, store, secrets=self.secret,
                                    echo=lambda m: print(m, flush=True))
        return self._factory


# ---------------------------------------------------------------------------- commands

def cmd_init(app: App) -> int:
    draft = setup_agent.detect(app.repo)
    text = setup_agent.render(draft)
    config_mod.load(text=text)  # validate the draft before writing anything
    if app.args.print:
        print(text)
        return 0
    app.state_dir.mkdir(parents=True, exist_ok=True)
    pending = app.state_dir / "factory.yaml"
    if pending.exists() and not app.args.force:
        print(f"{pending} exists; pass --force to overwrite")
        return 1
    pending.write_text(text)
    print(f"Drafted config: {pending}")
    app.register(name=app.args.name, priority=app.args.priority)
    print(f"Registered as {app.state_dir.name}; it appears in `asf ui`.")
    if not app.args.no_branch:
        commit = setup_agent.write_branch(app.repo, text, app.args.branch, app.state_dir)
        print(f"Committed it on branch {app.args.branch} ({commit[:10]}) without touching your checkout.")
        print("Review and merge that branch to make the config official. Until then the pending copy is used.")
    return 0


def cmd_doctor(app: App) -> int:
    ok = True

    def check(cond: bool, good: str, bad: str, fatal: bool = True) -> None:
        nonlocal ok
        print(("  ok    " if cond else ("  FAIL  " if fatal else "  warn  ")) + (good if cond else bad))
        if not cond and fatal:
            ok = False

    cfg = app.cfg
    print(f"config: {cfg.source} (policy version {cfg.version_hash})")
    check((app.repo / ".git").exists(), "git repository", f"{app.repo} is not a git repository")
    from .util import run
    check(run(["git", "-C", str(app.repo), "rev-parse", "--verify", cfg.default_branch], shell=False).ok,
          f"default branch {cfg.default_branch} exists", f"default branch {cfg.default_branch} not found")
    for p in cfg.packages:
        for kind, cmd in p.commands.items():
            exe = cmd.split()[0]
            found = shutil.which(exe) or (app.repo / p.root / exe).exists()
            check(bool(found), f"package {p.name}: {kind} -> {cmd}", f"package {p.name}: '{exe}' not on PATH")
    for d in cfg.get("execution.clone_dirs", []):
        check((app.repo / d).exists(), f"clone dir {d} present", f"clone dir {d} missing (run your install)",
              fatal=False)
    for kind in (cfg.get("intake") or {}):
        try:
            adapters.load("intake", kind)
            check(True, f"intake adapter {kind}", "")
        except KeyError as e:
            check(False, "", str(e))
    for cat, key in (("sandbox", "execution.provider"), ("delivery", "delivery.provider"),
                     ("models", "models.provider")):
        try:
            adapters.load(cat, cfg.get(key))
            check(True, f"{cat} adapter {cfg.get(key)}", "")
        except KeyError as e:
            check(False, "", str(e))
    if cfg.get("models.provider") == "anthropic":
        key_env = cfg.get("models.api_key_env", "ANTHROPIC_API_KEY")
        check(bool(app.secret(key_env)), f"{key_env} available (value not shown)",
              f"{key_env} not set; pass --env-file or export it", fatal=True)
    elif cfg.get("models.provider") == "pool":
        from .adapters.models.pool import PoolProvider
        pool = PoolProvider(cfg.get("models") or {}, None)
        for name, prov in pool.backends.items():
            problem = prov.credentials_problem()
            check(not problem, f"backend {name} ({prov.kind}) ready", f"backend {name}: {problem}", fatal=False)
        for role in ("triage", "implementer", "tester", "reviewer", "security", "ci_fixer"):
            print(f"  info  {role}: {' -> '.join(pool.order(role))}")
    elif cfg.get("models.provider") == "claude_code":
        from .adapters.models.claude_code import find_binary
        b = find_binary(cfg.get("models") or {})
        check(bool(b), f"Claude Code CLI at {b}", "Claude Code CLI not found: set models.binary or ASF_CLAUDE_BIN")
    print(f"  info  shadow mode {'on' if cfg.get('autonomy.shadow') else 'off'}, max merge tier "
          f"{cfg.get('autonomy.max_merge_tier')}, monthly cap ${cfg.get('budgets.monthly_cap_usd')}")
    if ok:
        app.register()
    print("healthy" if ok else "problems found")
    return 0 if ok else 1


def cmd_intake(app: App) -> int:
    jobs = app.factory.poll(app.args.source, app.args.id or None, rerun=app.args.rerun)
    print(f"{len(jobs)} new job(s): {', '.join(jobs) or '-'}")
    return 0


def cmd_add(app: App) -> int:
    item = WorkItem(source_kind="local", external_id=f"cli-{int(time.time())}", title=app.args.title,
                    body=app.args.body or "", kind=app.args.kind, labels=app.args.label or [],
                    acceptance=app.args.accept or [])
    job_id, _ = app.factory.submit(item)
    print(job_id)
    return 0


ACTIVE = ["queued", "triaged", "implementing", "testing", "verifying", "reviewing", "committing",
          "delivering", "awaiting_approval", "merging", "observing"]


def cmd_run(app: App) -> int:
    f = app.factory
    if app.args.job:
        targets = app.args.job
    else:
        targets = [j["id"] for j in f.store.jobs(ACTIVE)]
        if not app.args.all:
            targets = targets[: app.args.max_jobs]
    if not targets:
        print("nothing to run")
        return 0
    for job_id in targets:
        state = f.run_job(job_id, stop_at=app.args.stop_at)
        job = f.store.job(job_id)
        extra = job["data"].get("needs_human_reason") or job["data"].get("paused_reason") or \
            job["data"].get("error") or job["data"].get("outcome") or ""
        tier = job["tier"] if job["tier"] is not None else f"~{job['data'].get('pre_tier', '?')}"
        print(f"{job_id}: {state} (tier {tier}, ${f.store.job_cost(job_id):.2f}) {extra}")
    return 0


def cmd_status(app: App) -> int:
    s = app.store
    if app.args.job:
        job = s.job(app.args.job)
        item = s.get_work_item(job["work_item_id"])
        print(json.dumps({"id": job["id"], "state": job["state"], "tier": job["tier"],
                          "work_item": f"{item.source_kind}:{item.external_id} {item.title}",
                          "cost_usd": round(s.job_cost(job["id"]), 4), **job["data"]}, indent=2, default=str))
        return 0
    rows = s.jobs()
    if not rows:
        print("no jobs")
    for job in rows:
        item = s.get_work_item(job["work_item_id"])
        print(f"{job['id']:<11} {job['state']:<18} tier {job['tier'] if job['tier'] is not None else '-'}  "
              f"${s.job_cost(job['id']):>5.2f}  {item.source_kind}:{item.external_id}  {item.title[:60]}")
    return 0


def cmd_events(app: App) -> int:
    for e in app.store.events(app.args.job):
        if app.args.jsonl:
            print(json.dumps(e, default=str))
        else:
            p = e["payload"]
            brief = p.get("tool") and f"{p['tool']} {p.get('decision')} {p.get('reason', '')[:80]}" or \
                json.dumps(p, default=str)[:160]
            print(f"{e['time']} {e['type']:<16} {e['actor']:<12} {brief}")
    return 0


def cmd_report(app: App) -> int:
    from . import metrics
    m = metrics.compute(app.store)
    if getattr(app.args, "json", False):
        print(json.dumps(m, indent=2))
        return 0
    print(metrics.render_text(m))
    gate_status: Counter[str] = Counter()
    denials = 0
    for j in app.store.jobs():
        for g in j["data"].get("gates", []):
            gate_status[f"{g['name']}:{g['status']}"] += 1
    for e in app.store.events():
        if e["type"] == "tool.call" and e["payload"].get("decision") == "deny":
            denials += 1
    print("Gate outcomes: " + (", ".join(f"{k} {v}" for k, v in sorted(gate_status.items())) or "-"))
    print(f"Policy denials of agent tool calls: {denials}")
    return 0


def cmd_approve(app: App, decision: str) -> int:
    app.store.approve(app.args.job, app.args.by or os.environ.get("USER", "human"), decision, app.args.reason or "")
    print(f"{decision} recorded for {app.args.job}")
    return 0


def cmd_resume(app: App) -> int:
    app.factory.resume(app.args.job, app.args.extra_usd, app.args.extra_minutes)
    print(f"{app.args.job} resumed; run `asf run --job {app.args.job}`")
    return 0


def cmd_retry(app: App) -> int:
    state = app.factory.retry(app.args.job)
    print(f"{app.args.job} will resume at {state}; run `asf run --job {app.args.job}`")
    return 0


def cmd_revise(app: App) -> int:
    notes = list(app.args.note or [])
    if app.args.notes_file:
        text = Path(app.args.notes_file).expanduser().read_text()
        notes += [line.lstrip("-* ").strip() for line in text.splitlines() if line.strip()]
    state = app.factory.revise(app.args.job, notes, app.args.extra_usd)
    print(f"{app.args.job} will resume at {state} on its branch with {len(notes)} note(s); "
          f"run `asf run --job {app.args.job}`")
    return 0


def cmd_publish(app: App) -> int:
    res = app.factory.publish(app.args.job)
    print(f"{'updated' if res['updated'] else 'opened'} draft PR {res['url']} for {app.args.job}")
    return 0


def cmd_reviews(app: App) -> int:
    actions = app.factory.reviews(app.args.job, dry_run=app.args.dry_run)
    if not actions:
        print("no published jobs to check")
    for a in actions:
        print(f"{a['job']}  PR #{a['pr']}  {a['action']}" + (f" -> {a['result']}" if a.get("result") else ""))
        for n in a.get("notes", []):
            print(f"    - {n[:160]}")
    return 0


def cmd_cleanup(app: App) -> int:
    """Remove sandboxes of finished jobs and cached base checkouts. Branches are kept."""
    from .util import git
    f = app.factory
    removed = 0
    for job in f.store.jobs(["done", "needs_human", "failed", "rejected"]):
        info = job["data"].get("sandbox")
        if info and Path(info["root"]).exists():
            f._sandbox(job).destroy()
            removed += 1
    base_dir = app.state_dir / "sandboxes"
    for d in base_dir.glob("base-*") if base_dir.exists() else []:
        git(app.repo, "worktree", "remove", "--force", str(d), check=False)
        shutil.rmtree(d, ignore_errors=True)
        removed += 1
    git(app.repo, "worktree", "prune", check=False)
    print(f"removed {removed} sandbox(es); branches kept")
    return 0


def cmd_pause_all(app: App, on: bool) -> int:
    app.store.set_flag("paused_all", (app.args.reason or "paused by operator") if on else None)
    print("all jobs paused" if on else "factory resumed")
    return 0


def cmd_ui(app: App) -> int:
    from .ui import serve as serve_ui
    open_project = None
    if app.args.repo != "." or (app.repo / "factory.yaml").exists():
        app.factory  # registers this repo's state dir so the console lists it
        open_project = app.state_dir.name
    serve_ui(app.args.port, open_project=open_project, open_browser=app.args.open, secrets=app.secret)
    return 0


def cmd_serve(app: App) -> int:
    from .server import serve
    serve(app.factory, app.args.port, app.secret("ASF_ADMIN_TOKEN"), app.args.worker)
    return 0


def cmd_ci_workflow(app: App) -> int:
    from .ci import github_actions_workflow
    text = github_actions_workflow(app.cfg, app.args.install)
    if app.args.write:
        out = app.repo / ".github" / "workflows" / "software-factory.yml"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        print(f"wrote {out}")
    else:
        print(text)
    return 0


def cmd_adapters(app: App) -> int:
    for cat in ("intake", "sandbox", "delivery", "notify", "observe", "models"):
        print(f"{cat:<9} {', '.join(adapters.available(cat))}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="asf", description="Agentic software factory")
    p.add_argument("--repo", default=".", help="target repository (default: current directory)")
    p.add_argument("--config", help="path to factory.yaml (default: repo, then pending copy)")
    p.add_argument("--state", help="state directory (default: $ASF_HOME/<repo>)")
    p.add_argument("--env-file", help="file with secrets such as ANTHROPIC_API_KEY")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help="override a config value, e.g. delivery.provider=github")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="draft factory.yaml for the repo")
    s.add_argument("--branch", default="factory/setup")
    s.add_argument("--no-branch", action="store_true")
    s.add_argument("--print", action="store_true")
    s.add_argument("--force", action="store_true")
    s.add_argument("--name", help="display name in the console")
    s.add_argument("--priority", type=int, help="scheduler priority (lower runs first; default 100)")
    sub.add_parser("doctor", help="check config, commands, adapters and credentials")
    s = sub.add_parser("intake", help="pull work items from configured sources")
    s.add_argument("--source")
    s.add_argument("--id", action="append")
    s.add_argument("--rerun", action="store_true",
                   help="start a new job even if the item already has a finished one")
    s = sub.add_parser("add", help="queue a work item by hand")
    s.add_argument("--title", required=True)
    s.add_argument("--body")
    s.add_argument("--kind", default="task")
    s.add_argument("--label", action="append")
    s.add_argument("--accept", action="append")
    s = sub.add_parser("run", help="run queued jobs through the pipeline")
    s.add_argument("--job", action="append")
    s.add_argument("--all", action="store_true")
    s.add_argument("--max-jobs", type=int, default=1)
    s.add_argument("--stop-at", help="stop when a job reaches this state, e.g. triaged")
    s = sub.add_parser("status")
    s.add_argument("job", nargs="?")
    s = sub.add_parser("events")
    s.add_argument("job", nargs="?")
    s.add_argument("--jsonl", action="store_true")
    sub.add_parser("report", help="factory metrics (counts only shipped work)").add_argument("--json", action="store_true")
    p_ui = sub.add_parser("ui", help="local web console: observe jobs and act on them")
    p_ui.add_argument("--port", type=int, default=8787)
    p_ui.add_argument("--open", action="store_true", help="open the console in a browser")
    for name in ("approve", "reject"):
        s = sub.add_parser(name)
        s.add_argument("job")
        s.add_argument("--by")
        s.add_argument("--reason")
    s = sub.add_parser("resume")
    s.add_argument("job")
    s.add_argument("--extra-usd", type=float, default=0.0)
    s.add_argument("--extra-minutes", type=float, default=0.0,
        help="extend the time budget, e.g. after time spent paused")
    s = sub.add_parser("retry", help="re-enter a failed or needs_human job at the failing step")
    s.add_argument("job")
    s = sub.add_parser("revise", help="send a delivered, unmerged job back with review notes")
    s.add_argument("job")
    s.add_argument("--note", action="append", help="one revision note (repeatable)")
    s.add_argument("--notes-file", help="file with one note per line")
    s.add_argument("--extra-usd", type=float, default=0.0, help="budget on top of the tier cap")
    s = sub.add_parser("publish", help="push a delivered job's branch and open a draft pull request")
    s.add_argument("job")
    s = sub.add_parser("reviews", help="act on /revise comments on factory pull requests")
    s.add_argument("--job", help="only this job")
    s.add_argument("--dry-run", action="store_true", help="show what would happen; change nothing")
    sub.add_parser("cleanup", help="remove finished sandboxes (branches are kept)")
    s = sub.add_parser("pause-all", help="kill switch")
    s.add_argument("--reason")
    sub.add_parser("resume-all")
    s = sub.add_parser("serve", help="webhook gateway (+ optional worker)")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--worker", action="store_true")
    s = sub.add_parser("ci-workflow", help="GitHub Actions workflow for runner mode")
    s.add_argument("--write", action="store_true")
    s.add_argument("--install")
    sub.add_parser("adapters", help="list available adapters")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    app = App(args)
    handlers = {
        "init": cmd_init, "doctor": cmd_doctor, "intake": cmd_intake, "add": cmd_add, "run": cmd_run,
        "status": cmd_status, "events": cmd_events, "report": cmd_report, "ui": cmd_ui,
        "approve": lambda a: cmd_approve(a, "approve"), "reject": lambda a: cmd_approve(a, "reject"),
        "resume": cmd_resume, "retry": cmd_retry, "revise": cmd_revise, "publish": cmd_publish,
        "reviews": cmd_reviews, "cleanup": cmd_cleanup, "pause-all": lambda a: cmd_pause_all(a, True),
        "resume-all": lambda a: cmd_pause_all(a, False), "serve": cmd_serve,
        "ci-workflow": cmd_ci_workflow, "adapters": cmd_adapters,
    }
    try:
        return handlers[args.cmd](app)
    except config_mod.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    except ValueError as e:  # refused requests (retry, resume, revise on the wrong job)
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
