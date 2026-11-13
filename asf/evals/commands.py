"""`asf eval ...`: mine, check, run, report, list."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from ..util import git
from . import mine as mine_mod
from . import report as report_mod
from . import runner, score, variants, workspace
from .cases import EvalError, EvalSet, dump_case, evals_root, guard_archive


def add_parser(sub: Any) -> None:
    e = sub.add_parser("eval", help="measure the factory against hidden tests (docs/PARITY.md, Phase 0)")
    e.add_argument("--evals", help="the eval archive (default $ASF_EVALS or ~/work/asf-runs/evals)")
    es = e.add_subparsers(dest="eval_cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--evals", default=argparse.SUPPRESS, help="the eval archive")

    m = es.add_parser("mine", parents=[common], help="draft cases from merged history, for a human to prune")
    m.add_argument("--repo", dest="mine_repo", help="the repository to mine (default: --repo before `eval`)")
    m.add_argument("--since", help="only history since this date, e.g. 2026-01-01")
    m.add_argument("--limit", type=int, default=30, help="at most this many drafts (default 30)")
    m.add_argument("--max-lines", type=int, default=400, help="skip changes larger than this (default 400)")
    m.add_argument("--branch", help="branch to walk (default: the checked-out one)")
    m.add_argument("--set", dest="set_name", help="set to draft into (default: the repository's name)")
    m.add_argument("--fetch-issues", action="store_true", help="read referenced issues with the GitHub CLI")
    m.add_argument("--print", action="store_true", help="print the drafts; write nothing")

    c = es.add_parser("check", parents=[common], help="run each case's hidden tests on base (should fail) and reference (should pass)")
    c.add_argument("--set", dest="set_name", required=True)
    c.add_argument("--cases", nargs="+")

    r = es.add_parser("run", parents=[common], help="run cases through a variant and score them")
    r.add_argument("--set", dest="set_name", required=True)
    r.add_argument("--config", dest="variants", action="append", required=True, metavar="VARIANT",
                   help="variant name (repeatable, or comma-separated): current, claude_code, "
                        "claude_code_native, cursor_native, opus-high, interactive, or your own")
    r.add_argument("--cases", nargs="+", help="only these case ids")
    r.add_argument("--repeat", type=int, default=1, help="runs per case and variant (default 1)")
    r.add_argument("--jobs", type=int, default=1, help="runs in parallel (default 1)")
    r.add_argument("--max-usd", type=float, help="stop starting runs once this much was spent")
    r.add_argument("--no-wait", action="store_true", help="skip runs instead of waiting out usage limits")
    r.add_argument("--keep", action="store_true", help="keep checkouts and state folders for inspection")
    r.add_argument("--run-id", help="name for this run (default: a timestamp)")
    r.add_argument("--report", action="store_true", help="print the set's report afterwards")

    p = es.add_parser("report", parents=[common], help="per-variant tables; writes JSON and Markdown into the archive")
    p.add_argument("--set", dest="set_name", required=True)
    p.add_argument("--run", action="append", help="only these run ids (repeatable)")
    p.add_argument("--variant", action="append", help="only these variants (repeatable)")
    p.add_argument("--baseline", help="variant to compare against (default: interactive, if run)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-write", action="store_true", help="print only")

    ls = es.add_parser("list", parents=[common], help="sets, cases and variants")
    ls.add_argument("--set", dest="set_name")


def handle(args: argparse.Namespace) -> int:
    root = evals_root(args.evals)
    try:
        return {"mine": _mine, "check": _check, "run": _run, "report": _report, "list": _list}[args.eval_cmd](args, root)
    except EvalError as e:
        print(f"eval: {e}", file=sys.stderr)
        return 2


def _mine(args: argparse.Namespace, root: Path) -> int:
    repo = Path(args.mine_repo or args.repo).expanduser().resolve()
    globs = None
    if (repo / "factory.yaml").exists():
        globs = ((yaml.safe_load((repo / "factory.yaml").read_text()) or {}).get("tests") or {}).get("globs")
    drafts = mine_mod.mine(repo, since=args.since, limit=args.limit, max_lines=args.max_lines,
                           branch=args.branch or "", fetch_issues=args.fetch_issues, test_globs=globs)
    if args.print:
        for d in drafts:
            print("---\n" + dump_case(d))
        print(f"# {len(drafts)} draft(s)", file=sys.stderr)
        return 0
    guard_archive(root, [repo])
    es = EvalSet(root, args.set_name or repo.name)
    folder = es.dir / "drafts"
    folder.mkdir(parents=True, exist_ok=True)
    known = {p.stem for p in es.cases_dir.glob("*.y*ml")} if es.cases_dir.is_dir() else set()
    wrote = 0
    for d in drafts:
        if d.id in known:
            continue
        (folder / f"{d.id}.yaml").write_text(dump_case(d))
        wrote += 1
        m = d.extra["mined"]
        print(f"{d.id}: {m['changed_lines']} lines, {len(m['code_files'])} code / {len(m['test_files'])} test "
              f"file(s), ticket from {m['ticket_from']}")
    print(f"{wrote} draft(s) in {folder}. Rewrite each ticket as its requester would have, check it with "
          f"`asf eval check --set {es.name}` once it is in {es.cases_dir}, and delete the rest.")
    return 0


def _check(args: argparse.Namespace, root: Path) -> int:
    es = EvalSet(root, args.set_name)
    cases = es.cases(args.cases)
    guard_archive(root, [c.local_repo for c in cases if c.local_repo])
    bad = 0
    out_dir = es.dir / "checks"
    out_dir.mkdir(parents=True, exist_ok=True)
    for case in cases:
        work = workspace.eval_home() / "work" / es.name / "check" / case.id
        try:
            co, base = workspace.checkout(case, work / "repo")
            m = workspace.mirror(case)
            ref = workspace.resolve(m, case.reference)
            raw, source = workspace.base_config(case, co)
            cfg = workspace.eval_config(raw)
            workspace.prepare(case, co, cfg.raw)
            overlay, runnable = score.hidden_files(case, cfg, m, base, ref)
            on_base, _ = score.score(case, cfg, co, base, "", m, ref)
            ref_patch = git(m, "diff", "--binary", base, ref)
            on_ref, log = score.score(case, cfg, co, base, ref_patch, m, ref)
        except EvalError as e:
            print(f"FAIL  {case.id}: {e}")
            bad += 1
            continue
        finally:
            shutil.rmtree(work, ignore_errors=True)
        problems = []
        if not runnable:
            problems.append("no runnable hidden tests")
        if not on_ref.get("all_pass"):
            problems.append(f"hidden tests fail on the reference ({on_ref.get('passed')}/{on_ref.get('total')})")
        if on_base.get("all_pass"):
            problems.append("hidden tests already pass on base: they do not test the change")
        result = {"case": case.id, "checked": time.time(), "config": source, "hidden_tests": runnable,
                  "overlay": overlay, "on_base": on_base, "on_reference": on_ref, "problems": problems}
        (out_dir / f"{case.id}.json").write_text(json.dumps(result, indent=1))
        if problems:
            bad += 1
            print(f"FAIL  {case.id}: {'; '.join(problems)}")
            if not on_ref.get("all_pass"):
                print("      " + log[-1500:].replace("\n", "\n      "))
        else:
            print(f"ok    {case.id}: {len(runnable)} hidden test file(s); base {on_base.get('passed')}/"
                  f"{on_base.get('total')}, reference {on_ref.get('passed')}/{on_ref.get('total')}")
    return 1 if bad else 0


def _run(args: argparse.Namespace, root: Path) -> int:
    es = EvalSet(root, args.set_name)
    cases = es.cases(args.cases)
    names = [n.strip() for v in args.variants for n in v.split(",") if n.strip()]
    vs = [variants.load(root, n) for n in names]
    if args.repeat < 1 or args.jobs < 1:
        raise EvalError("--repeat and --jobs must be at least 1")
    opts = runner.Options(repeat=args.repeat, jobs=args.jobs, keep=args.keep, max_usd=args.max_usd,
                          wait=not args.no_wait, run_id=args.run_id or "",
                          echo=lambda msg: print(msg, flush=True))
    results = runner.run_set(es, vs, cases, opts)
    errors = [r for r in results if r.get("final_state") == "harness_error"]
    print(f"run {opts.run_id}: {len(results)} result(s), {len(errors)} harness error(s), "
          f"${opts.spent:.2f} spent. `asf eval report --set {es.name}` compares variants.")
    if args.report:
        rep = report_mod.summarize(report_mod.load_results(es))
        print(report_mod.render(rep, es.name))
        report_mod.write(es, rep)
    return 1 if errors and len(errors) == len(results) else 0


def _report(args: argparse.Namespace, root: Path) -> int:
    es = EvalSet(root, args.set_name)
    results = report_mod.load_results(es, args.run, args.variant)
    if not results:
        raise EvalError(f"no results for set {es.name} in {es.runs_dir}; run `asf eval run` first")
    rep = report_mod.summarize(results, args.baseline)
    if args.json:
        print(json.dumps(rep, indent=1, default=str))
    else:
        print(report_mod.render(rep, es.name))
    if not args.no_write:
        js, md = report_mod.write(es, rep)
        print(f"wrote {js} and {md}", file=sys.stderr)
    return 0


def _list(args: argparse.Namespace, root: Path) -> int:
    print(f"archive: {root}")
    print("variants:")
    for name, v in variants.available(root).items():
        why = v.unavailable({})
        print(f"  {name:<20} {v.kind:<12} {v.description}" + (f"  [unavailable: {why}]" if why else ""))
    sets = [args.set_name] if args.set_name else sorted(p.name for p in (root / "sets").glob("*") if p.is_dir())
    for s in sets:
        es = EvalSet(root, s)
        n = len(list(es.cases_dir.glob("*.y*ml"))) if es.cases_dir.is_dir() else 0
        drafts = len(list((es.dir / "drafts").glob("*.y*ml"))) if (es.dir / "drafts").is_dir() else 0
        runs = len(list(es.runs_dir.glob("*"))) if es.runs_dir.is_dir() else 0
        print(f"set {s}: {n} case(s), {drafts} draft(s), {runs} run(s)")
        if args.set_name and n:
            for c in es.cases():
                print(f"  {c.id:<50} {c.ticket.kind:<8} {c.ticket.title[:60]}")
    return 0
