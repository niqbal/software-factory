"""`asf eval site`: one static page over every eval run in the archive.

Pattern: every finished run sample becomes one row in a flat table (set, run, variant, case,
repeat, outcome and measures, plus the code version and variant hash that produced it). The page
is only views over that table: the run in progress, comparisons across variants, history across
runs, per-case detail and the written reports. History stays comparable because a row never
changes once written, and the version columns say what changed between two runs.

The data is embedded in the page, so it opens from disk with no server; `--watch` rebuilds it
every few seconds and the page reloads itself while a run is in progress.
"""
from __future__ import annotations

import difflib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

from .report import corrected, sample_values

LIMIT = 60_000  # characters of one file or diff shown in the page

TEMPLATE = Path(__file__).with_name("site.html")
# Files of a run that the page links to, in this order: what the agent changed, the factory's
# delivery report, the hidden test output, the run's own log, every job event.
ARTIFACTS = ["candidate.diff", "report.md", "hidden.log", "run.log", "events.jsonl", "factory.yaml"]
# A run folder with no result.json whose files changed within this long is shown as running.
ACTIVE_SECONDS = 45 * 60


def _read(p: Path) -> Any:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def outcome(r: dict[str, Any], v: dict[str, float | None]) -> str:
    """resolved | partial (delivered, hidden tests not all passing) | not_delivered | settled | error."""
    if r.get("settled"):
        return "settled"
    if r.get("error") or r.get("skipped"):
        return "error"
    if v["resolved"] == 1.0:
        return "resolved"
    if r.get("delivered"):
        return "partial"
    return "not_delivered"


def sample_row(r: dict[str, Any], set_name: str, asf: str, path: str = "") -> dict[str, Any]:
    v = sample_values(r)
    h = r.get("hidden") or {}
    return {
        "set": set_name, "run": r.get("run_id"), "variant": r.get("variant"), "case": r.get("case"),
        "repeat": r.get("repeat"), "outcome": outcome(r, v),
        "resolved": v["resolved"], "hidden_rate": v["hidden_rate"], "visible": v["visible"], "gap": v["gap"],
        "first_try": v["first_try"], "fix_rounds": v["fix_rounds"], "denials": v["denials"],
        "cost_usd": round(v["cost_usd"], 4), "cost_estimated": bool(r.get("cost_estimated")),
        "minutes": None if v["minutes"] is None else round(v["minutes"], 2), "size_ratio": v["size_ratio"],
        "final_state": r.get("final_state") or "", "stop_reason": (r.get("stop_reason") or r.get("error") or "")[:300],
        "fixed": h.get("fixed"), "fail_to_pass": h.get("fail_to_pass"), "broken": len(h.get("broken") or []),
        "started": r.get("started"), "finished": r.get("finished"),
        "variant_hash": r.get("variant_hash"), "asf": asf, "job": r.get("job_id"),
        "path": path, "files": [],
    }


def _active(run_dir: Path, now: float) -> list[dict[str, Any]]:
    out = []
    for d in run_dir.glob("*/*/"):
        if (d / "result.json").exists() or not d.is_dir():
            continue
        files = [f for f in d.rglob("*") if f.is_file()]
        if not files:
            continue
        last = max(f.stat().st_mtime for f in files)
        if now - last > ACTIVE_SECONDS:
            continue
        step = ""
        ev = d / "events.jsonl"
        if ev.exists():
            lines = ev.read_text(errors="replace").strip().splitlines()
            if lines:
                e = _read_line(lines[-1])
                step = (e.get("type") or "") if e else ""
        name = d.name
        case, _, rep = name.rpartition("-r")
        out.append({"variant": d.parent.name, "case": case, "repeat": int(rep) if rep.isdigit() else None,
                    "since": min(f.stat().st_mtime for f in files), "updated": last, "step": step})
    return out


def _read_line(s: str) -> dict[str, Any] | None:
    try:
        return json.loads(s)
    except ValueError:
        return None


def collect(root: Path) -> dict[str, Any]:
    now = time.time()
    data: dict[str, Any] = {"generated": now, "sets": [], "runs": [], "samples": [], "reports": []}
    for sdir in sorted((root / "sets").glob("*/")):
        cases = {}
        for p in sorted((sdir / "cases").glob("*.y*ml")):
            try:
                c = yaml.safe_load(p.read_text()) or {}
            except Exception:  # noqa: BLE001 - a broken case file should not hide the rest
                continue
            t = c.get("ticket") or {}
            cases[c.get("id") or p.stem] = {"title": t.get("title", ""), "tags": c.get("tags") or [],
                                            "reference_by": c.get("reference_by", ""), "kind": t.get("kind", "")}
        data["sets"].append({"name": sdir.name, "cases": cases})
        for rdir in sorted((sdir / "runs").glob("*/")):
            meta = _read(rdir / "run.json") or {}
            asf = meta.get("asf", "")
            n = 0
            for f in sorted(rdir.glob("*/*/result.json")):
                r = _read(f)
                if r:
                    r = corrected(r, f.parent)
                    row = sample_row(r, sdir.name, asf, str(f.parent.relative_to(root)))
                    row["files"] = [a for a in ARTIFACTS if (f.parent / a).exists()]
                    data["samples"].append(row)
                    n += 1
            infra = len(list(rdir.glob("*/*/result.infra-failed.json")))
            plan = meta.get("plan") or {v: {"cases": meta.get("cases", []), "repeat": meta.get("repeat", 1)}
                                        for v in meta.get("variants", {})}
            data["runs"].append({
                "set": sdir.name, "run": rdir.name, "started": meta.get("started"), "finished": meta.get("finished"),
                "asf": asf, "max_usd": meta.get("max_usd"), "plan": plan,
                "variants": {k: v.get("hash") for k, v in (meta.get("variants") or {}).items()},
                "paused": sorted(p.name for p in rdir.glob("PAUSE*")),
                "active": _active(rdir, now), "samples": n, "infra_retried": infra,
            })
        for p in sorted((sdir / "reports").glob("*.md")):
            if p.name.startswith("latest"):
                continue
            data["reports"].append({"set": sdir.name, "name": p.stem, "md": p.read_text(errors="replace")})
    for p in sorted(root.glob("*.md")):
        data["reports"].append({"set": "", "name": p.stem, "md": p.read_text(errors="replace")})
    return data


def _git(repo: Path | str, *args: str, limit: int = 0) -> str:
    try:
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    out = p.stdout if p.returncode == 0 else ""
    return out[:limit] + f"\n... ({len(out) - limit} more characters)" if limit and len(out) > limit else out


def _fresh(target: Path, key: str) -> bool:
    """True when `target` was built from the same inputs (`key`) and need not be rebuilt."""
    old = _read(target)
    return bool(old) and old.get("key") == key


def _write(target: Path, data: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.replace(target)


def case_detail(root: Path, set_name: str, case_file: Path) -> dict[str, Any]:
    """What the agent was given (ticket, repo at base) and what it never saw (hidden tests, reference fix)."""
    from .cases import load_case
    case = load_case(case_file)
    chk = _read(root / "sets" / set_name / "checks" / f"{case.id}.json") or {}
    repo = case.local_repo
    base, ref = chk.get("base_sha") or case.base, chk.get("reference_sha") or case.reference
    hidden_dir = case.hidden_folder
    files = []
    for path in chk.get("hidden_tests") or []:
        authored = hidden_dir / path if hidden_dir else None
        if authored and authored.exists():
            text, source = authored.read_text(errors="replace")[:LIMIT], "written for this eval (in the case folder)"
        else:
            text, source = _git(repo, "show", f"{ref}:{path}", limit=LIMIT) if repo else "", "from the reference commit"
        at_base = bool(repo) and bool(_git(repo, "cat-file", "-t", f"{base}:{path}").strip())
        diff = _git(repo, "diff", base, ref, "--", path, limit=LIMIT) if repo and at_base and not (authored and authored.exists()) else ""
        files.append({"path": path, "source": source, "at_base": at_base, "text": text, "diff_from_base": diff})
    hidden = set(chk.get("hidden_tests") or [])
    ref_fix = ""
    if repo:
        patch = case.reference_patch_file
        if patch and patch.exists():
            ref_fix = patch.read_text(errors="replace")[:LIMIT * 2]
        else:
            ref_fix = _git(repo, "diff", base, ref, "--", ".", *[f":(exclude){h}" for h in hidden], limit=LIMIT * 2)
    p2p = chk.get("pass_to_pass") or []
    return {
        "id": case.id, "repo": Path(case.repo).name, "base": base, "reference": ref,
        "ticket": {"title": case.ticket.title, "body": case.ticket.body, "acceptance": case.ticket.acceptance,
                   "kind": case.ticket.kind},
        "prompt": case.ticket.text(), "notes": case.notes, "tags": case.tags, "reference_by": case.reference_by,
        "fail_to_pass": chk.get("fail_to_pass") or [], "pass_to_pass": len(p2p) if isinstance(p2p, list) else p2p,
        "flaky": chk.get("flaky") or [], "warnings": chk.get("warnings") or [], "checked": chk.get("checked"),
        "hidden_files": files, "reference_fix": ref_fix, "repo_found": bool(repo),
    }


def changes(runs: list[dict[str, Any]], root: Path) -> list[dict[str, Any]]:
    """Between each eval run and the one before it: factory commits, and each variant's settings diff."""
    code = Path(__file__).resolve().parents[2]
    origin = _git(code, "remote", "get-url", "origin").strip()
    web = ""
    if "github.com" in origin:
        web = "https://github.com/" + origin.split("github.com")[1].lstrip(":/").removesuffix(".git") + "/commit/"
    out = []
    for prev, cur in zip(runs, runs[1:]):
        commits = []
        if prev["asf"] and cur["asf"] and prev["asf"] != cur["asf"]:
            log = _git(code, "log", "--reverse", "--format=%H%x1f%s%x1f%b%x1f%ad%x1e", "--date=short",
                       f"{prev['asf']}..{cur['asf']}")
            for entry in filter(str.strip, log.split("\x1e")):
                sha, subject, body, date = (entry.strip("\n").split("\x1f") + ["", "", "", ""])[:4]
                stat = _git(code, "show", "--format=", "--numstat", sha)
                commits.append({"sha": sha, "subject": subject, "body": body.strip(), "date": date,
                                "files": [line.split("\t")[::-1][0] + f"  +{line.split(chr(9))[0]} -{line.split(chr(9))[1]}"
                                          for line in stat.splitlines() if line.count("\t") >= 2],
                                "patch": _git(code, "show", "--format=", sha, "--", "asf", "docs", "README.md", limit=LIMIT),
                                "url": web + sha if web else ""})
        settings = {}
        for v in sorted(set(prev["variants"]) & set(cur["variants"])):
            a, b = _variant_config(root, prev, v), _variant_config(root, cur, v)
            if a != b:
                settings[v] = "".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True),
                                                           f"{prev['run']}/{v}", f"{cur['run']}/{v}"))
        out.append({"from": prev["run"], "to": cur["run"], "from_asf": prev["asf"], "to_asf": cur["asf"],
                    "commits": commits, "settings": settings,
                    "unknown_code": bool(prev["asf"] and cur["asf"] and prev["asf"] != cur["asf"] and not commits)})
    return out


def _variant_config(root: Path, run: dict[str, Any], variant: str) -> str:
    """The factory.yaml one of the variant's runs used, with per-job paths taken out so runs compare."""
    rdir = root / "sets" / run["set"] / "runs" / run["run"] / variant
    for f in sorted(rdir.glob("*/factory.yaml")):
        try:
            cfg = yaml.safe_load(f.read_text()) or {}
        except yaml.YAMLError:
            continue
        for k in ("project", "repo", "paths", "state_dir", "workspace"):
            cfg.pop(k, None)
        return yaml.safe_dump(cfg, sort_keys=True)
    return ""


def build_details(root: Path, out: Path, data: dict[str, Any]) -> None:
    for s in data["sets"]:
        for f in sorted((root / "sets" / s["name"] / "cases").glob("*.y*ml")):
            chk = root / "sets" / s["name"] / "checks" / f"{f.stem}.json"
            key = f"{f.stat().st_mtime}-{chk.stat().st_mtime if chk.exists() else 0}"
            target = out / "cases" / s["name"] / f"{f.stem}.json"
            if _fresh(target, key):
                continue
            try:
                _write(target, {"key": key, **case_detail(root, s["name"], f)})
            except Exception as e:  # noqa: BLE001 - one bad case must not stop the page
                _write(target, {"key": key, "id": f.stem, "error": f"{type(e).__name__}: {e}"})
        runs = [r for r in data["runs"] if r["set"] == s["name"] and r["samples"]]
        runs.sort(key=lambda r: r.get("started") or 0)
        key = json.dumps([(r["run"], r["asf"], r["variants"]) for r in runs])
        target = out / "changes" / f"{s['name']}.json"
        if not _fresh(target, key):
            _write(target, {"key": key, "changes": changes(runs, root)})


def build(root: Path, out: Path) -> Path:
    data = collect(root)
    build_details(root, out, data)
    page = TEMPLATE.read_text().replace("/*__DATA__*/null", json.dumps(data, separators=(",", ":"))
                                        .replace("</", "<\\/"))
    out.mkdir(parents=True, exist_ok=True)
    target = out / "index.html"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(page)
    tmp.replace(target)
    return target
