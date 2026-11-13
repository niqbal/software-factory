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
    from .setup_wizard import project_checks, ready
    cfg = app.cfg
    print(f"config: {cfg.source} (policy version {cfg.version_hash})")
    checks = project_checks(app.repo, app.state_dir, secret=app.secret, cfg=cfg)
    print_checks([c for c in checks if c.id != "config"])
    ok = ready(checks)
    if ok:
        app.register()
    print("healthy" if ok else "problems found")
    return 0 if ok else 1


LABELS = {"ok": "  ok    ", "fail": "  FAIL  ", "warn": "  warn  ", "info": "  info  "}


def print_checks(checks: list[Any], fixes: bool = False) -> None:
    for c in checks:
        print(f"{LABELS.get(c.status, '        ')}{c.label}" + (f": {c.detail}" if c.detail else ""))
        if fixes and c.fix and c.status != "ok":
            print(f"          fix: {c.fix}")


def _ask(prompt: str, default: str = "") -> str:
    try:
        answer = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def _yes(prompt: str, default: bool = False) -> bool:
    answer = _ask(prompt + (" (Y/n)" if default else " (y/N)")).lower()
    return default if not answer else answer.startswith("y")


def _setup_options(args: argparse.Namespace) -> dict[str, Any]:
    return {"name": args.name, "priority": args.priority, "backend": args.backend,
            "github_issues": args.github_issues, "label": args.label, "write_back": args.write_back,
            "github_prs": args.github_prs, "branch": not args.no_branch, "force": args.force}


def _print_draft(d: dict[str, Any]) -> None:
    s = d["summary"]
    print(f"\nProject {s['name']} ({d['repo']}), default branch {s['default_branch']}")
    print(f"  stacks: {', '.join(s['stacks']) or 'none detected'}; backend: {s['backend']}; "
          f"delivery: {s['delivery']}; intake: {', '.join(s['intake'])}")
    for p in s["packages"]:
        cmds = ", ".join(f"{k}: {v}" for k, v in p["commands"].items()) or "no commands"
        print(f"  package {p['name']} ({p['root']}): {cmds}")
    print(f"  owner-only (tier 3) paths: {', '.join(s['owner_only_paths'][:8])}"
          + (" ..." if len(s["owner_only_paths"]) > 8 else ""))
    print(f"  agents read: {', '.join(s['instruction_files']) or 'no instruction files'}")
    print("  shadow mode: nothing merges until you raise autonomy.max_merge_tier")
    for w in d["warnings"]:
        print(f"  warn  {w}")
    for t in d["tips"]:
        print(f"  tip   {t}")


def _next_steps(result: dict[str, Any]) -> None:
    repo = result["repo"]
    print("\nNext:")
    if result.get("branch"):
        print(f"  review the draft:  git -C {repo} diff {result['summary']['default_branch']}...{result['branch']}")
    print(f"  give it work:      asf --repo {repo} add --title \"...\" --accept \"...\"")
    print("  open the console:  asf ui --open      (start the scheduler there)")


def cmd_setup(app: App) -> int:
    from . import setup_wizard as sw
    sub = app.args.setup_cmd
    if sub == "check" or (sub is None and not sys.stdin.isatty()):
        checks = sw.machine_checks()
        if getattr(app.args, "json", False):
            print(json.dumps({"ready": sw.ready(checks), "checks": [c.as_dict() for c in checks]}, indent=2))
        else:
            print("This machine:")
            print_checks(checks, fixes=True)
            print("ready" if sw.ready(checks) else "not ready: fix the FAIL lines above")
        return 0 if sw.ready(checks) else 1
    if sub == "add":
        repo = Path(app.args.path or app.args.repo).expanduser().resolve()
        opts = _setup_options(app.args)
        if app.args.print:
            d = sw.draft(repo, opts)
            print(json.dumps(d, indent=2) if app.args.json else d["yaml"])
            return 0
        result = sw.create(repo, opts)
        if app.args.json:
            print(json.dumps(result, indent=2, default=str))
            return 0 if result["healthy"] else 1
        _print_draft(result)
        print()
        for n in result["notes"]:
            print(f"  done  {n}")
        print("\nProject checks:")
        print_checks([sw.Check(**c) for c in result["checks"]], fixes=True)
        _next_steps(result)
        return 0 if result["healthy"] else 1
    return _setup_wizard(app)


def _setup_wizard(app: App) -> int:
    from . import setup_wizard as sw
    print("Software factory setup\n\n1. This machine")
    checks = sw.machine_checks()
    print_checks(checks, fixes=True)
    if not sw.ready(checks):
        print("\nFix the FAIL lines above, then run `asf setup` again.")
        return 1
    print("\n2. Add a project")
    found = sw.candidate_repos()
    for i, r in enumerate(found[:20], 1):
        print(f"  {i:>2}) {r['path']}" + ("  (already added)" if r["project"] else ""))
    pick = _ask("Repository: a number above or a path (Enter to skip)")
    if not pick:
        print("No project added. Run `asf setup add PATH` later, or use the console: asf ui --setup")
        return 0
    repo = Path(found[int(pick) - 1]["path"]) if pick.isdigit() and 0 < int(pick) <= len(found[:20]) \
        else Path(pick).expanduser().resolve()
    problem = sw.validate_repo(repo)
    if problem:
        print(problem)
        return 1
    opts: dict[str, Any] = {"name": _ask("Display name", repo.name),
                            "priority": int(_ask("Priority (lower runs first)", "100") or 100)}
    if _yes("Change the optional settings (backend, GitHub)?"):
        opts["backend"] = _ask(f"Model backend ({'/'.join(sw.BACKENDS)})", "auto")
        opts["github_issues"] = _yes("Take work from GitHub issues with a label?")
        if opts["github_issues"]:
            opts["label"] = _ask("Label", "factory")
            opts["write_back"] = _yes("Comment results back on the issue?")
        opts["github_prs"] = _yes("Deliver as GitHub draft pull requests (instead of local branches)?")
        opts["branch"] = _yes("Commit the draft factory.yaml on branch factory/setup?", True)
    d = sw.draft(repo, opts)
    _print_draft(d)
    if not _yes("\nAdd this project?", True):
        print("Nothing written.")
        return 0
    result = sw.create(repo, opts)
    for n in result["notes"]:
        print(f"  done  {n}")
    print("\n3. Project checks")
    print_checks([sw.Check(**c) for c in result["checks"]], fixes=True)
    print("\n4. Give it work")
    title = _ask("First work item title (Enter to skip)")
    if title:
        accept = _ask("Acceptance criteria, separated by ';'")
        sub_app = App(argparse.Namespace(**{**vars(app.args), "repo": str(repo), "state": None}))
        job = sw.first_item(sub_app.factory, title, acceptance=accept.split(";"))
        print(f"  queued {job}")
    _next_steps(result)
    if _yes("\nOpen the console now?", True):
        from .ui import serve as serve_ui
        serve_ui(8787, open_project=result["key"], open_browser=True, secrets=app.secret)
    return 0


def _fmt_issue_item(kind: str, item: WorkItem) -> str:
    url = item.url or "-"
    return f"- [{kind}] #{item.external_id} {item.title} ({url})"


def cmd_intake(app: App) -> int:
    f = app.factory
    adapters_by_kind = f.intake_adapters()
    requested = app.args.source
    targets = [(k, a) for k, a in adapters_by_kind.items() if not requested or k == requested]

    jobs: list[str] = []
    listed_external = 0
    saw_external = False
    errors = 0

    for kind, adapter in targets:
        if kind == "local":
            try:
                jobs.extend(f.poll(kind, app.args.id or None, rerun=app.args.rerun))
            except Exception as e:  # noqa: BLE001 - keep intake resilient and explicit
                print(f"intake error [{kind}]: {e}")
                errors += 1
            continue

        saw_external = True
        try:
            items = [adapter.fetch(i) for i in app.args.id] if app.args.id else adapter.poll()
        except Exception as e:  # noqa: BLE001 - one source failure should not crash intake
            print(f"intake error [{kind}]: {e}")
            errors += 1
            continue

        if not items:
            print(f"[{kind}] no issues available")
            continue

        print(f"[{kind}] {len(items)} issue(s)")
        for item in items:
            listed_external += 1
            print(_fmt_issue_item(kind, item))
            job_id, new_job = f.submit(item, rerun=app.args.rerun)
            if new_job:
                print(f"  queued {job_id}")
                jobs.append(job_id)
            else:
                state = f.store.job(job_id)["state"]
                print(f"  skip   already has {job_id} ({state})")

    if saw_external and listed_external == 0 and errors == 0:
        print("no external issues found")

    print(f"{len(jobs)} new job(s): {', '.join(jobs) or '-'}")
    return 1 if errors else 0


def cmd_add(app: App) -> int:
    item = WorkItem(source_kind="local", external_id=f"cli-{int(time.time())}", title=app.args.title,
                    body=app.args.body or "", kind=app.args.kind, labels=app.args.label or [],
                    acceptance=app.args.accept or [])
    job_id, _ = app.factory.submit(item)
    print(job_id)
    return 0


ACTIVE = ["queued", "triaged", "reproducing", "implementing", "testing", "verifying", "confirming", "reviewing", "committing",
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


def cmd_deploy(app: App) -> int:
    ids = app.factory.approve_deploy(app.args.job, by=app.args.by or os.environ.get("USER", "owner"))
    for jid in ids:
        print(f"{jid} -> {app.factory.run_job(jid)}")
    return 0


def cmd_resume(app: App) -> int:
    app.factory.resume(app.args.job, app.args.extra_usd, app.args.extra_minutes)
    print(f"{app.args.job} resumed; run `asf run --job {app.args.job}`")
    return 0


def cmd_retry(app: App) -> int:
    state = app.factory.retry(app.args.job)
    print(f"{app.args.job} will resume at {state}; run `asf run --job {app.args.job}`")
    return 0


def cmd_answer(app: App) -> int:
    print(f"{app.args.job}: {app.factory.answer(app.args.job, app.args.text, by=app.args.by or 'human')}; "
          f"run `asf run --job {app.args.job}`")
    return 0


def cmd_repro(app: App) -> int:
    """Run committed English reproductions in a browser (model time) on the default branch."""
    if not app.args.all and not app.args.files:
        print("name reproductions (tests/repro/<slug>.md or <slug>) or pass --all", file=sys.stderr)
        return 2
    results = app.factory.repro_suite(None if app.args.all else app.args.files, ref=app.args.ref)
    if not results:
        print("no reproductions found (packages[].commands.repro names the folder)")
        return 0
    for r in results:
        seen = "; ".join(f"{m['name']}={m['value']} (expected {m['expected']})" for m in r["measurements"] or [])
        print(f"{'FAIL' if r['reproduced'] else 'ok  '}  {r['md']}: {r['verdict']}  {seen}")
        if r["reproduced"] and r["notes"]:
            print(f"      {r['notes'][:300]}")
    print(f"evidence: {Path(results[0]['evidence']).parent}")
    return 1 if any(r["reproduced"] for r in results) else 0


def cmd_e2e(app: App) -> int:
    """Run the e2e suites on the default branch, with the app started for them."""
    results = app.factory.e2e_suite(ref=app.args.ref)
    if not results:
        print("no package declares an e2e command")
        return 0
    for r in results:
        print(f"{'ok  ' if r['ok'] else 'FAIL'}  {r['package']}")
        if not r["ok"] or app.args.verbose:
            print(r["output"])
    return 0 if all(r["ok"] for r in results) else 1


def cmd_allow_repro_edit(app: App) -> int:
    state = app.factory.allow_repro_edit(app.args.job, by=app.args.by or os.environ.get("USER", "owner"))
    print(f"{app.args.job}: the implementer may change its reproduction test; resumes at {state}. "
          f"Run `asf run --job {app.args.job}`")
    return 0


def cmd_rerun(app: App) -> int:
    new = app.factory.rerun(app.args.job, by=app.args.by or "human")
    print(f"{app.args.job} replaced by {new} (from the current default branch); run `asf run --job {new}`")
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
    serve_ui(app.args.port, open_project=open_project, open_browser=app.args.open or app.args.setup,
             secrets=app.secret, fragment="setup" if app.args.setup else None)
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


def cmd_eval(app: App) -> int:
    from .evals import commands as eval_commands
    return eval_commands.handle(app.args)


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
    p_ui.add_argument("--setup", action="store_true", help="open the console on its Set up page")
    s = sub.add_parser("setup", help="check this machine and add a project (a wizard without arguments)")
    ss = s.add_subparsers(dest="setup_cmd")
    c = ss.add_parser("check", help="check this machine: Python, git, model backends, optional tools")
    c.add_argument("--json", action="store_true")
    a = ss.add_parser("add", help="add a repository as a project, with optional settings")
    a.add_argument("path", nargs="?", help="the repository (default: --repo)")
    a.add_argument("--name", help="display name in the console")
    a.add_argument("--priority", type=int, help="lower runs first when projects share a subscription")
    a.add_argument("--backend", default="auto", choices=["auto", "claude", "cursor", "pool", "anthropic"],
                   help="model backend (auto: the signed-in CLIs on this machine)")
    a.add_argument("--github-issues", action="store_true", help="take work from labelled GitHub issues")
    a.add_argument("--label", default="factory", help="issue label for --github-issues")
    a.add_argument("--write-back", action="store_true", help="comment results on the issue")
    a.add_argument("--github-prs", action="store_true", help="deliver as GitHub draft pull requests")
    a.add_argument("--no-branch", action="store_true", help="do not commit the draft on factory/setup")
    a.add_argument("--force", action="store_true", help="replace an existing pending draft")
    a.add_argument("--print", action="store_true", help="show the draft; write nothing")
    a.add_argument("--json", action="store_true")
    for name in ("approve", "reject"):
        s = sub.add_parser(name)
        s.add_argument("job")
        s.add_argument("--by")
        s.add_argument("--reason")
    s = sub.add_parser("deploy", help="run the deploy steps for a job awaiting deploy (and every other one)")
    s.add_argument("job")
    s.add_argument("--by")
    s = sub.add_parser("resume")
    s.add_argument("job")
    s.add_argument("--extra-usd", type=float, default=0.0)
    s.add_argument("--extra-minutes", type=float, default=0.0,
        help="extend the time budget, e.g. after time spent paused")
    s = sub.add_parser("retry", help="re-enter a failed or needs_human job at the failing step")
    s.add_argument("job")
    s = sub.add_parser("answer", help="answer the question a stopped job asks, and let it continue")
    s.add_argument("job")
    s.add_argument("text")
    s.add_argument("--by")
    s = sub.add_parser("repro", help="run committed English reproductions in a browser (uses model time)")
    s.add_argument("files", nargs="*", help="tests/repro/<slug>.md paths or slugs")
    s.add_argument("--all", action="store_true", help="every reproduction in every package's repro folder")
    s.add_argument("--ref", help="branch or commit to check (default: the default branch)")
    s = sub.add_parser("e2e", help="run the e2e suites with the app started for them")
    s.add_argument("--ref", help="branch or commit to check (default: the default branch)")
    s.add_argument("-v", "--verbose", action="store_true")
    s = sub.add_parser("allow-repro-edit", help="let a job's implementer change its committed reproduction test")
    s.add_argument("job")
    s.add_argument("--by")
    s = sub.add_parser("rerun", help="replace a stopped job with a new one for the same item, from the default branch")
    s.add_argument("job")
    s.add_argument("--by")
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
    from .evals import commands as eval_commands
    eval_commands.add_parser(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    app = App(args)
    handlers = {
        "init": cmd_init, "doctor": cmd_doctor, "setup": cmd_setup, "intake": cmd_intake, "add": cmd_add, "run": cmd_run,
        "status": cmd_status, "events": cmd_events, "report": cmd_report, "ui": cmd_ui,
        "approve": lambda a: cmd_approve(a, "approve"), "reject": lambda a: cmd_approve(a, "reject"),
        "deploy": cmd_deploy, "resume": cmd_resume, "retry": cmd_retry, "rerun": cmd_rerun, "answer": cmd_answer,
        "repro": cmd_repro, "e2e": cmd_e2e, "allow-repro-edit": cmd_allow_repro_edit, "revise": cmd_revise, "publish": cmd_publish,
        "reviews": cmd_reviews, "cleanup": cmd_cleanup, "pause-all": lambda a: cmd_pause_all(a, True),
        "resume-all": lambda a: cmd_pause_all(a, False), "serve": cmd_serve,
        "ci-workflow": cmd_ci_workflow, "adapters": cmd_adapters, "eval": cmd_eval,
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
