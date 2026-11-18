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

import json
import time
from pathlib import Path
from typing import Any

from .report import sample_values

TEMPLATE = Path(__file__).with_name("site.html")
# A run folder with no result.json whose files changed within this long is shown as running.
ACTIVE_SECONDS = 45 * 60


def _read(p: Path) -> Any:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def outcome(r: dict[str, Any], v: dict[str, float | None]) -> str:
    """resolved | partial (delivered, hidden tests not all passing) | not_delivered | error."""
    if r.get("error") or r.get("skipped"):
        return "error"
    if v["resolved"] == 1.0:
        return "resolved"
    if r.get("delivered"):
        return "partial"
    return "not_delivered"


def sample_row(r: dict[str, Any], set_name: str, asf: str) -> dict[str, Any]:
    v = sample_values(r)
    h = r.get("hidden") or {}
    return {
        "set": set_name, "run": r.get("run_id"), "variant": r.get("variant"), "case": r.get("case"),
        "repeat": r.get("repeat"), "outcome": outcome(r, v),
        "resolved": v["resolved"], "hidden_rate": v["hidden_rate"], "visible": v["visible"], "gap": v["gap"],
        "first_try": v["first_try"], "fix_rounds": v["fix_rounds"], "denials": v["denials"],
        "cost_usd": round(v["cost_usd"], 4), "cost_estimated": bool(r.get("cost_estimated")),
        "minutes": round(v["minutes"], 2), "size_ratio": v["size_ratio"],
        "final_state": r.get("final_state") or "", "stop_reason": (r.get("stop_reason") or r.get("error") or "")[:300],
        "fixed": h.get("fixed"), "fail_to_pass": h.get("fail_to_pass"), "broken": len(h.get("broken") or []),
        "started": r.get("started"), "finished": r.get("finished"),
        "variant_hash": r.get("variant_hash"), "asf": asf, "job": r.get("job_id"),
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
                import yaml
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
                    data["samples"].append(sample_row(r, sdir.name, asf))
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


def build(root: Path, out: Path) -> Path:
    data = collect(root)
    page = TEMPLATE.read_text().replace("/*__DATA__*/null", json.dumps(data, separators=(",", ":"))
                                        .replace("</", "<\\/"))
    out.mkdir(parents=True, exist_ok=True)
    target = out / "index.html"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(page)
    tmp.replace(target)
    return target
