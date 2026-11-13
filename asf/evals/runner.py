"""Run eval cases: each case x variant x repeat in its own clean checkout, then score it.

Factory variants go through the real pipeline (`asf.orchestrator.Factory`) as a throwaway
project with its own state folder, in shadow mode with local-branch delivery: nothing merges and
nothing is pushed. The interactive baseline runs `claude -p` in the checkout instead, then the
same deterministic gates the factory would have run decide its "visible" pass.

Usage limits are respected the way the scheduler respects them (asf/limits.py): before each case
the run waits for a backend to be free, and a job stopped by a limit waits for the reset and is
retried. Per-job budgets are the variant's factory.yaml budgets; `max_usd` caps the whole run.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml

from .. import limits, policy
from ..adapters import base as adapters
from ..adapters.base import AdapterContext
from ..adapters.sandbox.local_worktree import WorktreeSandbox
from ..config import Config
from ..gates import GateRunner
from ..models import WorkItem
from ..orchestrator import Factory
from ..projects import record
from ..setup_wizard import code_root, first_item
from ..store import Store
from ..util import git, run
from . import score, workspace
from .cases import Case, EvalError, EvalSet, guard_archive
from .variants import INTERACTIVE_TOOLS, Variant

SCRUB_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")


class LimitStop(RuntimeError):
    """Every backend the variant needs is at its usage limit, and the run was told not to wait."""


@dataclass
class Options:
    repeat: int = 1
    jobs: int = 1
    keep: bool = False
    max_usd: float | None = None
    wait: bool = True
    run_id: str = ""
    echo: Callable[[str], None] = print
    sleep: Callable[[float], None] = time.sleep
    spent: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def code_version() -> str:
    root = code_root()
    head = run(["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"], shell=False)
    if not head.ok:
        return "unknown"
    dirty = run(["git", "-C", str(root), "status", "--porcelain", "--", "asf"], shell=False).stdout.strip()
    return head.stdout.strip() + ("+dirty" if dirty else "")


def run_set(evalset: EvalSet, variants: list[Variant], cases: list[Case], opts: Options) -> list[dict[str, Any]]:
    """Run every case under every variant `opts.repeat` times. Returns the result records."""
    guard_archive(evalset.root, [c.local_repo for c in cases if c.local_repo])
    opts.run_id = opts.run_id or new_run_id()
    run_dir = evalset.runs_dir / opts.run_id
    runnable = []
    for v in variants:
        why = v.unavailable({})
        if why:
            opts.echo(f"skip variant {v.name}: {why}")
        else:
            runnable.append(v)
    if not runnable:
        raise EvalError("no variant can run in this build of asf")
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = {"run_id": opts.run_id, "set": evalset.name, "started": time.time(), "asf": code_version(),
            "variants": {v.name: {"kind": v.kind, "hash": v.hash, "source": v.source} for v in runnable},
            "cases": [c.id for c in cases], "repeat": opts.repeat, "jobs": opts.jobs, "max_usd": opts.max_usd}
    (run_dir / "run.json").write_text(json.dumps(meta, indent=1))
    tasks = [(c, v, k) for k in range(1, opts.repeat + 1) for c in cases for v in runnable]
    opts.echo(f"eval run {opts.run_id}: {len(cases)} case(s) x {len(runnable)} variant(s) x {opts.repeat} "
              f"= {len(tasks)} run(s), {opts.jobs} at a time; results in {run_dir}")

    def go(task: tuple[Case, Variant, int]) -> dict[str, Any]:
        c, v, k = task
        return run_one(evalset, c, v, k, opts)

    if opts.jobs > 1:
        with ThreadPoolExecutor(max_workers=opts.jobs) as pool:
            results = list(pool.map(go, tasks))
    else:
        results = [go(t) for t in tasks]
    meta.update(finished=time.time(), spent_usd=round(opts.spent, 4), results=len(results))
    (run_dir / "run.json").write_text(json.dumps(meta, indent=1))
    return results


def run_one(evalset: EvalSet, case: Case, variant: Variant, k: int, opts: Options) -> dict[str, Any]:
    tag = f"[{variant.name} {case.id} r{k}]"
    lines: list[str] = []

    def echo(msg: str) -> None:
        lines.append(f"{datetime.now():%H:%M:%S} {msg}")
        opts.echo(f"{tag} {msg}")

    out_dir = evalset.runs_dir / opts.run_id / variant.name / f"{case.id}-r{k}"
    out_dir.mkdir(parents=True, exist_ok=True)
    work = workspace.eval_home() / "work" / evalset.name / opts.run_id / variant.name / f"{case.id}-r{k}"
    result: dict[str, Any] = {"case": case.id, "variant": variant.name, "kind": variant.kind, "repeat": k,
                              "run_id": opts.run_id, "set": evalset.name, "variant_hash": variant.hash,
                              "started": time.time()}
    with opts._lock:
        over = opts.max_usd is not None and opts.spent >= opts.max_usd
    if over:
        result.update(skipped=f"eval budget of ${opts.max_usd:.2f} reached")
        echo(result["skipped"])
        return _save(out_dir, result, lines)
    try:
        if variant.kind == "interactive":
            result.update(_interactive(case, variant, work, out_dir, opts, echo))
        else:
            result.update(_factory(case, variant, work, out_dir, opts, echo))
    except LimitStop as e:
        result.update(skipped=str(e))
        echo(f"skipped: {e}")
    except EvalError as e:
        result.update(error=str(e), final_state="harness_error")
        echo(f"error: {e}")
    except Exception as e:  # noqa: BLE001 - one broken case never stops the set
        result.update(error=f"{type(e).__name__}: {e}", final_state="harness_error",
                      trace=traceback.format_exc()[-3000:])
        echo(f"harness error: {e}")
    finally:
        if not opts.keep and work.exists():
            shutil.rmtree(work, ignore_errors=True)
    with opts._lock:
        opts.spent += float(result.get("cost_usd") or 0)
    result["finished"] = time.time()
    if not result.get("skipped") and not result.get("error"):
        h = result.get("hidden") or {}
        echo(f"{result.get('final_state')}: hidden {h.get('passed')}/{h.get('total')}, visible "
             f"{'pass' if result.get('visible_pass') else 'fail'}, ${result.get('cost_usd', 0):.2f}, "
             f"{result.get('minutes', 0):.1f} min")
    if opts.keep:
        result["work_dir"] = str(work)
    return _save(out_dir, result, lines)


def _save(out_dir: Path, result: dict[str, Any], lines: list[str]) -> dict[str, Any]:
    (out_dir / "result.json").write_text(json.dumps(result, indent=1, default=str))
    (out_dir / "run.log").write_text("\n".join(lines) + "\n")
    return result


# ----------------------------------------------------------------------------- shared steps

def _setup(case: Case, variant: Variant | None, work: Path, out_dir: Path,
           echo: Callable[[str], None]) -> tuple[Path, str, Config, Path, str]:
    """Clean checkout, config, dependencies. Returns (checkout, base, config, mirror, reference)."""
    echo(f"checkout {case.repo} at {case.base[:12]}")
    co, base = workspace.checkout(case, work / "repo")
    m = workspace.mirror(case)
    ref = workspace.resolve(m, case.reference)
    raw, source = workspace.base_config(case, co)
    if variant is not None:
        why = variant.unavailable(raw)
        if why:
            raise EvalError(why)
        raw = variant.apply(raw)
    cfg = workspace.eval_config(raw)
    (out_dir / "factory.yaml").write_text(f"# base config: {source}\n" + yaml.safe_dump(cfg.raw, sort_keys=False))
    for note in workspace.prepare(case, co, cfg.raw):
        echo(note)
    return co, base, cfg, m, ref


def _score(case: Case, cfg: Config, co: Path, base: str, patch: str, m: Path, ref: str, out_dir: Path,
           echo: Callable[[str], None]) -> dict[str, Any]:
    (out_dir / "candidate.diff").write_text(patch)
    ref_patch = git(m, "diff", "--binary", base, ref)
    diff = score.compare(score.numstat(patch, cfg), score.numstat(ref_patch, cfg))
    echo("scoring against the hidden tests")
    hidden, log = score.score(case, cfg, co, base, patch, m, ref)
    (out_dir / "hidden.log").write_text(log)
    return {"hidden": hidden, "diff": diff, "base_sha": base, "reference_sha": ref}


def _wait_for_backends(backends: list[str], opts: Options, echo: Callable[[str], None]) -> float:
    """Sleep while every backend is at its usage limit. Returns seconds waited."""
    waited = 0.0
    while True:
        until = [limits.limited_until(b) for b in backends]
        if not backends or any(u is None for u in until):
            return waited
        lift = min(u for u in until if u is not None)
        if not opts.wait:
            raise LimitStop(f"usage limit on {', '.join(backends)} until {lift:%H:%M}")
        secs = max(30.0, (lift - datetime.now().astimezone()).total_seconds())
        echo(f"usage limit on {', '.join(backends)}; waiting until {lift:%H:%M}")
        opts.sleep(min(secs, 600))
        waited += min(secs, 600)


# ----------------------------------------------------------------------------- factory variants

def _factory(case: Case, variant: Variant, work: Path, out_dir: Path, opts: Options,
             echo: Callable[[str], None]) -> dict[str, Any]:
    co, base, cfg, m, ref = _setup(case, variant, work, out_dir, echo)
    state = work / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "factory.yaml").write_text(yaml.safe_dump(cfg.raw, sort_keys=False))
    record(state, co)  # a throwaway project: its own state folder, outside $ASF_HOME's project list
    store = Store(state / "factory.db")
    f = Factory(co, cfg, state, store, echo=echo)
    t = case.ticket
    job_id = first_item(f, t.title, t.body, t.acceptance, t.kind)
    backends = variant.backends(cfg.raw)
    echo(f"{job_id} submitted; running to delivery (shadow mode)")
    started = time.time()
    waited, limit_hits = 0.0, 0
    while True:
        w = _wait_for_backends(backends, opts, echo)
        if w:
            waited += w
            job = store.job(job_id)
            store.update_job(job_id, extra_minutes=job["data"].get("extra_minutes", 0) + w / 60)
        state_now = f.run_job(job_id)
        job = store.job(job_id)
        err = job["data"].get("error") or ""
        if state_now == "failed" and limits.is_limit_error(err):
            limit_hits += 1
            limits.record_limit(limits.backend_of_error(err), limits.reset_time(err), err.split(": ", 1)[-1])
            echo(f"stopped by a usage limit: {err[:200]}")
            if not opts.wait or limit_hits > 5:
                break
            f.retry(job_id, keep_work=True)
            continue
        if state_now in ("failed", "needs_human") and f.auto_retry(job_id):
            echo(f"automatic retry after: {(job['data'].get('error') or job['data'].get('needs_human_reason') or '')[:200]}")
            continue
        break
    minutes = (time.time() - started - waited) / 60
    metrics = score.job_metrics(store, job_id)
    with open(out_dir / "events.jsonl", "w") as fh:
        for e in store.events(job_id):
            fh.write(json.dumps(e, default=str) + "\n")
    (out_dir / "runs.json").write_text(json.dumps(store.runs(job_id), indent=1, default=str))
    report = store.job(job_id)["data"].get("report")
    if report and Path(report).exists():
        shutil.copy(report, out_dir / "report.md")
    patch = _candidate_patch(f, store.job(job_id), co, base, cfg, metrics)
    out = {**metrics, "minutes": round(minutes, 2), "waited_minutes": round(waited / 60, 1),
           "limit_hits": limit_hits, "config_hash": cfg.version_hash}
    out.update(_score(case, cfg, co, base, patch, m, ref, out_dir, echo))
    return out


def _candidate_patch(f: Factory, job: dict[str, Any], co: Path, base: str, cfg: Config,
                     metrics: dict[str, Any]) -> str:
    """The factory's work: its delivered branch, else whatever is left in the job's sandbox."""
    branch = metrics.get("branch")
    if metrics["delivered"] and branch and run(["git", "-C", str(co), "rev-parse", "--verify", "-q",
                                                 f"refs/heads/{branch}"], shell=False).ok:
        return workspace.tree_patch(co, base, cfg, ref=f"refs/heads/{branch}")
    sb = job["data"].get("sandbox") or {}
    root = Path(sb["root"]) if sb.get("root") else None
    if root and root.exists():
        return workspace.tree_patch(root, base, cfg)
    return ""


# ----------------------------------------------------------------------------- interactive baseline

def _interactive(case: Case, variant: Variant, work: Path, out_dir: Path, opts: Options,
                 echo: Callable[[str], None]) -> dict[str, Any]:
    from ..adapters.models.claude_code import find_binary
    co, base, cfg, m, ref = _setup(case, None, work, out_dir, echo)
    s = variant.settings
    binary = find_binary({"binary": s.get("binary")} if s.get("binary") else {})
    if not binary:
        raise EvalError("the interactive baseline needs the Claude Code CLI (claude) on PATH or settings.binary")
    args = [binary, "-p", "--output-format", "json",
            "--permission-mode", str(s.get("permission_mode") or "acceptEdits"),
            "--allowedTools", ",".join(s.get("allowed_tools") or INTERACTIVE_TOOLS)]
    if s.get("model"):
        args += ["--model", str(s["model"])]
    if s.get("effort"):
        args += ["--effort", str(s["effort"])]
    args += [str(a) for a in s.get("extra_args") or []]
    env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}
    timeout = int(s.get("timeout_s", 3600))
    prompt = case.ticket.text()
    started = time.time()
    waited, cost, turns, denials, limit_hits = 0.0, 0.0, 0, 0, 0
    final, outcome, d = "error", "", {}
    while True:
        waited += _wait_for_backends(["claude"], opts, echo)
        echo("claude -p on the ticket text (interactive baseline)")
        try:
            p = subprocess.run(args, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=co, env=env)
            stdout, stderr = p.stdout, p.stderr
        except subprocess.TimeoutExpired as e:
            stdout = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = f"timed out after {timeout}s"
        try:
            d = json.loads(stdout)
        except json.JSONDecodeError:
            d = {}
        if not isinstance(d, dict) or not d:
            d = {"is_error": True, "result": (stderr or stdout)[-2000:]}
        cost += float(d.get("total_cost_usd") or 0)
        turns += int(d.get("num_turns") or 0)
        denials += len(d.get("permission_denials") or [])
        text = str(d.get("result") or "") + "\n" + stderr
        if d.get("is_error") and limits.is_limit_error(text):
            limit_hits += 1
            limits.record_limit("claude", limits.reset_time(text), text.strip()[:300])
            echo(f"stopped by a usage limit: {text.strip()[:200]}")
            if not opts.wait or limit_hits > 5:
                final, outcome = "limited", text.strip()[:500]
                break
            workspace.reset(co, base, cfg)
            git(co, "checkout", "-q", "-B", workspace.BRANCH, base)
            continue
        final = "error" if d.get("is_error") else "finished"
        outcome = text.strip()[:4000]
        break
    minutes = (time.time() - started - waited) / 60
    (out_dir / "claude.json").write_text(json.dumps(d, indent=1, default=str))
    patch = workspace.tree_patch(co, base, cfg)
    gates, visible = _baseline_gates(case, cfg, co, base, work / "state", patch)
    if final == "finished" and not patch.strip():
        final = "no_changes"
    out = {"final_state": final, "delivered": bool(patch.strip()) and final == "finished",
           "visible_pass": visible, "gates": gates, "gates_first_try": visible if gates else None,
           "fix_rounds": 0, "rounds": {}, "review_blocking": 0, "denials": denials, "cost_usd": round(cost, 4),
           "turns": turns, "auto_retries": 0, "stop_reason": outcome if final != "finished" else "",
           "summary": outcome[:2000], "minutes": round(minutes, 2), "waited_minutes": round(waited / 60, 1),
           "limit_hits": limit_hits, "config_hash": cfg.version_hash}
    out.update(_score(case, cfg, co, base, patch, m, ref, out_dir, echo))
    return out


def _baseline_gates(case: Case, cfg: Config, co: Path, base: str, state: Path,
                    patch: str) -> tuple[list[dict[str, Any]], bool]:
    """The deterministic gates the factory would run on this change: the baseline's 'visible' pass."""
    if not patch.strip():
        return [], False
    state.mkdir(parents=True, exist_ok=True)
    store = Store(state / "factory.db")
    ctx = AdapterContext(repo=co, state_dir=state, config=cfg, store=store)
    provider = adapters.load("sandbox", "local_worktree")(cfg.get("execution"), ctx)
    sb = WorktreeSandbox(co, co, workspace.BRANCH, base, cfg.get("execution"))
    item = WorkItem("local", "eval", case.ticket.title, body=case.ticket.body, kind=case.ticket.kind)
    tier = policy.final_tier(cfg, item, policy.pre_tier(cfg, item), sb.changed_files())
    decision = policy.decide(cfg, tier)
    results = GateRunner(cfg, sb, tier, state, base_sandbox=lambda: provider.base_checkout(base)).run(
        decision.deterministic)
    rows = [{"name": r.name, "package": r.package, "status": r.status, "detail": r.detail[:300]} for r in results]
    return rows, not any(r.blocking for r in results)
