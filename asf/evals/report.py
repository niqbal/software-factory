"""Compare variants on an eval set.

Every number is a mean over cases (each case weighs the same, whatever its repeats), shown
with the number of cases behind it. Measures per run:

- resolved: delivered, and every hidden test passes
- hidden pass: share of hidden tests that pass on the candidate
- visible pass: the factory's own deterministic gates passed (for the interactive baseline, the
  same gates run on what it left behind)
- gap: passes its own checks but fails a hidden test (the reward-hacking signal SpecBench names)
- gates first try, fix rounds, policy denials, dollars (list price), minutes (waits excluded)

Differences against the baseline variant are paired by case. One is flagged as noise when it
is under two standard errors of the per-case differences, and as untrustworthy below three
shared cases.
"""

from __future__ import annotations

import json
import math
import time
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, stdev
from typing import Any, Callable

from .cases import EvalSet

RATE_METRICS = ["resolved", "hidden_rate", "visible", "gap", "first_try"]
NUMBER_METRICS = ["fix_rounds", "denials", "cost_usd", "minutes", "size_ratio"]
LABELS = {"resolved": "resolved", "hidden_rate": "hidden pass", "visible": "visible pass",
          "gap": "visible-hidden gap", "first_try": "gates 1st try", "fix_rounds": "fix rounds",
          "denials": "denials", "cost_usd": "$ / run", "minutes": "min / run", "size_ratio": "size vs ref"}
COMPARE = ["resolved", "hidden_rate", "gap", "visible", "cost_usd", "minutes"]
MIN_CASES = 3


def load_results(evalset: EvalSet, runs: list[str] | None = None,
                 variants: list[str] | None = None) -> list[dict[str, Any]]:
    out = []
    if not evalset.runs_dir.is_dir():
        return out
    for p in sorted(evalset.runs_dir.glob("*/*/*/result.json")):
        r = json.loads(p.read_text())
        if runs and r.get("run_id") not in runs:
            continue
        if variants and r.get("variant") not in variants:
            continue
        r["_path"] = str(p.parent)
        out.append(r)
    return out


def sample_values(r: dict[str, Any]) -> dict[str, float | None]:
    """One run's measures, None where it does not apply."""
    h = r.get("hidden") or {}
    has_hidden = h.get("rate") is not None
    all_pass = bool(h.get("all_pass"))
    visible = bool(r.get("visible_pass"))
    ft = r.get("gates_first_try")
    size = (r.get("diff") or {}).get("size_ratio")
    return {
        "resolved": float(bool(r.get("delivered")) and all_pass) if has_hidden else None,
        "hidden_rate": float(h["rate"]) if has_hidden else None,
        "visible": float(visible),
        "gap": float(visible and not all_pass) if has_hidden else None,
        "first_try": None if ft is None else float(bool(ft)),
        "fix_rounds": float(r.get("fix_rounds") or 0),
        "denials": float(r.get("denials") or 0),
        "cost_usd": float(r.get("cost_usd") or 0),
        "minutes": float(r.get("minutes") or 0),
        "size_ratio": None if size is None else float(size),
    }


def _stat(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"mean": None, "n": 0, "se": None}
    se = stdev(values) / math.sqrt(len(values)) if len(values) > 1 else None
    return {"mean": round(mean(values), 4), "n": len(values), "se": None if se is None else round(se, 4)}


def per_case(results: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, list[float]]]]:
    """variant -> case -> metric -> values over repeats (skipped and harness errors left out)."""
    out: dict[str, dict[str, dict[str, list[float]]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for r in results:
        if r.get("skipped") or r.get("final_state") == "harness_error":
            continue
        for k, v in sample_values(r).items():
            if v is not None:
                out[r["variant"]][r["case"]][k].append(v)
    return out


def summarize(results: list[dict[str, Any]], baseline: str | None = None) -> dict[str, Any]:
    pc = per_case(results)
    variants = sorted({r["variant"] for r in results})
    summary: dict[str, Any] = {}
    for v in variants:
        rows = [r for r in results if r["variant"] == v]
        ok = [r for r in rows if not r.get("skipped") and r.get("final_state") != "harness_error"]
        cases = pc.get(v, {})
        metrics = {}
        for m in RATE_METRICS + NUMBER_METRICS:
            case_means = [mean(vals[m]) for vals in cases.values() if vals.get(m)]
            metrics[m] = _stat(case_means)
        # Run-to-run noise: spread of the hidden pass rate between repeats of the same case.
        spreads = [stdev(vals["hidden_rate"]) for vals in cases.values() if len(vals.get("hidden_rate", [])) > 1]
        repeats = Counter((r["case"]) for r in ok)
        summary[v] = {
            "kind": rows[0].get("kind", "factory"), "cases": len(cases), "runs": len(ok),
            "repeats": max(repeats.values()) if repeats else 0,
            "skipped": sum(1 for r in rows if r.get("skipped")),
            "harness_errors": sum(1 for r in rows if r.get("final_state") == "harness_error"),
            "states": dict(Counter(r.get("final_state", "?") for r in ok)),
            "spent_usd": round(sum(float(r.get("cost_usd") or 0) for r in ok), 2),
            "repeat_noise": round(mean(spreads), 4) if spreads else None,
            "metrics": metrics,
        }
    if baseline is None:
        baseline = "interactive" if "interactive" in variants else (variants[0] if variants else None)
    comparisons = {}
    if baseline in pc:
        for v in variants:
            if v == baseline:
                continue
            comparisons[v] = {m: paired(pc[v], pc[baseline], m) for m in COMPARE}
    flags = []
    n_cases = max((s["cases"] for s in summary.values()), default=0)
    if n_cases < 10:
        flags.append(f"Only {n_cases} case(s): small differences cannot be told from noise; PARITY.md "
                     "asks for 20 to 30 cases.")
    if summary and max(s["repeats"] for s in summary.values()) < 2:
        flags.append("No repeats: run-to-run variance is not measured (use --repeat 2 or more).")
    for v, s in summary.items():
        if s["skipped"] or s["harness_errors"]:
            flags.append(f"{v}: {s['skipped']} skipped and {s['harness_errors']} harness error(s) are "
                         "left out of the numbers.")
    return {"generated": time.time(), "baseline": baseline, "variants": summary, "comparisons": comparisons,
            "flags": flags, "per_case": _case_table(pc)}


def paired(a: dict[str, dict[str, list[float]]], b: dict[str, dict[str, list[float]]], metric: str) -> dict[str, Any]:
    """Mean per-case difference a - b on the cases both ran, with a verdict on whether it is real."""
    diffs = [mean(a[c][metric]) - mean(b[c][metric]) for c in a
             if c in b and a[c].get(metric) and b[c].get(metric)]
    n = len(diffs)
    if not n:
        return {"diff": None, "n": 0, "se": None, "verdict": "no shared cases"}
    d = mean(diffs)
    se = stdev(diffs) / math.sqrt(n) if n > 1 else None
    if n < MIN_CASES:
        verdict = f"too few cases ({n})"
    elif se == 0 or se is None:
        verdict = "no difference" if d == 0 else "consistent"
    elif abs(d) < 2 * se:
        verdict = "within noise"
    else:
        verdict = "likely real"
    return {"diff": round(d, 4), "n": n, "se": None if se is None else round(se, 4), "verdict": verdict}


def _case_table(pc: dict[str, dict[str, dict[str, list[float]]]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = defaultdict(dict)
    for v, cases in pc.items():
        for c, vals in cases.items():
            res = vals.get("resolved", [])
            out[c][v] = {"resolved": f"{int(sum(res))}/{len(res)}" if res else "-",
                         "hidden_rate": round(mean(vals["hidden_rate"]), 3) if vals.get("hidden_rate") else None}
    return dict(out)


# ----------------------------------------------------------------------------- rendering

def _fmt(metric: str, st: dict[str, Any]) -> str:
    if st["mean"] is None:
        return "-"
    m = st["mean"]
    if metric in RATE_METRICS:
        s = f"{m * 100:.0f}%" + (f" ±{st['se'] * 196:.0f}" if st["se"] else "")
    elif metric == "cost_usd":
        s = f"${m:.2f}"
    elif metric == "size_ratio":
        s = f"{m:.2f}x"
    else:
        s = f"{m:.1f}"
    return f"{s} ({st['n']})"


def _fmt_diff(metric: str, c: dict[str, Any]) -> str:
    if c["diff"] is None:
        return "-"
    d = c["diff"]
    if metric in RATE_METRICS:
        s = f"{d * 100:+.0f} pts"
    elif metric == "cost_usd":
        s = f"{'+' if d >= 0 else '-'}${abs(d):.2f}"
    else:
        s = f"{d:+.1f}"
    return f"{s}, {c['verdict']} (n={c['n']})"


def _table(header: list[str], rows: list[list[str]], md: bool) -> str:
    if md:
        lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
        lines += ["| " + " | ".join(r) + " |" for r in rows]
        return "\n".join(lines)
    widths = [max(len(str(x)) for x in col) for col in zip(header, *rows)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    return "\n".join([fmt.format(*header), fmt.format(*["-" * w for w in widths])] + [fmt.format(*r) for r in rows])


def render(rep: dict[str, Any], set_name: str, md: bool = False) -> str:
    h: Callable[[str], str] = (lambda t: f"## {t}") if md else (lambda t: f"{t}\n{'=' * len(t)}")
    out = [f"# Eval report: {set_name}" if md else f"Eval report: {set_name}", ""]
    out.append("Means over cases; the number in brackets is how many cases each figure rests on; "
               "± is a 95% interval across cases.")
    out.append("")
    out.append(h("Quality"))
    header = ["variant", "cases", "runs"] + [LABELS[m] for m in ("resolved", "hidden_rate", "visible", "gap",
                                                                 "first_try", "size_ratio")]
    rows = []
    for v, s in rep["variants"].items():
        rows.append([v, str(s["cases"]), str(s["runs"])] + [_fmt(m, s["metrics"][m]) for m in
                     ("resolved", "hidden_rate", "visible", "gap", "first_try", "size_ratio")])
    out += [_table(header, rows, md), ""]
    out.append(h("Cost and effort"))
    header = ["variant", "$ / run", "min / run", "fix rounds", "denials", "spent", "final states"]
    rows = []
    for v, s in rep["variants"].items():
        m = s["metrics"]
        states = ", ".join(f"{k} {n}" for k, n in sorted(s["states"].items()))
        rows.append([v, _fmt("cost_usd", m["cost_usd"]), _fmt("minutes", m["minutes"]),
                     _fmt("fix_rounds", m["fix_rounds"]), _fmt("denials", m["denials"]),
                     f"${s['spent_usd']:.2f}", states or "-"])
    out += [_table(header, rows, md), ""]
    if rep["comparisons"]:
        out.append(h(f"Against {rep['baseline']} (paired by case)"))
        header = ["variant"] + [LABELS[m] for m in COMPARE]
        rows = [[v] + [_fmt_diff(m, c[m]) for m in COMPARE] for v, c in rep["comparisons"].items()]
        out += [_table(header, rows, md), ""]
    if rep["per_case"]:
        out.append(h("Per case (resolved runs / runs, hidden pass rate)"))
        variants = list(rep["variants"])
        rows = []
        for case, by_v in sorted(rep["per_case"].items()):
            rows.append([case] + [(f"{by_v[v]['resolved']} {by_v[v]['hidden_rate'] * 100:.0f}%"
                                   if v in by_v and by_v[v]["hidden_rate"] is not None else
                                   (by_v[v]["resolved"] if v in by_v else "-")) for v in variants])
        out += [_table(["case"] + variants, rows, md), ""]
    if rep["flags"]:
        out.append(h("Read with care"))
        out += [f"- {f}" for f in rep["flags"]]
        out.append("")
    return "\n".join(out)


def write(evalset: EvalSet, rep: dict[str, Any]) -> tuple[Path, Path]:
    d = evalset.reports_dir
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    js = json.dumps(rep, indent=1, default=str)
    md = render(rep, evalset.name, md=True)
    for name in (stamp, "latest"):
        (d / f"{name}.json").write_text(js)
        (d / f"{name}.md").write_text(md)
    return d / f"{stamp}.json", d / f"{stamp}.md"
