"""Factory metrics that count only shipped work.

A job is shipped when it is `done`: merged and verified after the merge. Everything else is
work in progress or waste. Spend is divided by shipped jobs, not by deliveries, so abandoned
and stuck work shows up as a higher cost per shipped item instead of disappearing.
"""

from __future__ import annotations

from statistics import median
from typing import Any

from .store import Store

BUCKETS = {
    "shipped": {"done"},
    "verifying": {"merged", "observing", "merging", "deploying"},
    "awaiting_review": {"awaiting_review", "awaiting_approval", "awaiting_deploy"},
    "in_progress": {"queued", "triaged", "reproducing", "implementing", "testing", "verifying", "confirming", "reviewing",
                    "committing", "delivering"},
    "stopped": {"needs_human", "failed", "paused"},
    "abandoned": {"rejected"},
}
STOPS = {"needs_human", "failed", "paused"}


def bucket_of(state: str) -> str:
    for name, states in BUCKETS.items():
        if state in states:
            return name
    return "in_progress"


def _minutes(a: float | None, b: float | None) -> float | None:
    return round((b - a) / 60, 1) if a and b and b >= a else None


def _med(xs: list[float | None]) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(median(xs), 1) if xs else None


def compute(store: Store) -> dict[str, Any]:
    jobs = store.jobs()
    cost = {j["id"]: 0.0 for j in jobs}
    for r in store.runs():
        cost[r["job_id"]] = cost.get(r["job_id"], 0.0) + r["cost_usd"]
    stops: dict[str, int] = {j["id"]: 0 for j in jobs}
    for e in store.events():
        if e["type"] == "job.state" and e["payload"].get("to") in STOPS:
            stops[e["job_id"]] = stops.get(e["job_id"], 0) + 1

    counts = {b: 0 for b in BUCKETS}
    spend = {b: 0.0 for b in BUCKETS}
    lead, agent, review, verify = [], [], [], []
    for j in jobs:
        b = bucket_of(j["state"])
        counts[b] += 1
        spend[b] += cost.get(j["id"], 0.0)
        d = j["data"]
        if j["state"] == "done" and not d.get("migrated"):  # migrated jobs predate the timestamps
            lead.append(_minutes(j["created"], d.get("done_at")))
            agent.append(_minutes(j["created"], d.get("delivered_at")))
            review.append(_minutes(d.get("delivered_at"), d.get("merged_at")))
            verify.append(_minutes(d.get("merged_at"), d.get("done_at")))

    total = round(sum(cost.values()), 2)
    shipped = counts["shipped"]
    return {
        "jobs": len(jobs),
        "counts": counts,
        "spend_usd": {k: round(v, 2) for k, v in spend.items()} | {"total": total},
        "cost_per_shipped_usd": round(total / shipped, 2) if shipped else None,
        "waste_share": round(spend["abandoned"] / total, 3) if total else 0.0,
        "median_minutes": {"lead_time": _med(lead), "agent_time": _med(agent),
                           "review_wait": _med(review), "post_merge_verify": _med(verify)},
        "interventions": sum(stops.values()),
        "interventions_per_shipped": round(sum(stops.values()) / shipped, 2) if shipped else None,
        "per_job": {j["id"]: {"cost_usd": round(cost.get(j["id"], 0.0), 2), "stops": stops.get(j["id"], 0)}
                    for j in jobs},
    }


def render_text(m: dict[str, Any]) -> str:
    c, s, t = m["counts"], m["spend_usd"], m["median_minutes"]
    fmt = lambda v: "-" if v is None else v  # noqa: E731
    lines = [
        f"Jobs: {m['jobs']}  shipped {c['shipped']}, verifying {c['verifying']}, "
        f"awaiting review {c['awaiting_review']}, "
        f"in progress {c['in_progress']}, stopped {c['stopped']}, abandoned {c['abandoned']}",
        f"Spend: ${s['total']:.2f} total; ${s['shipped']:.2f} on shipped work, "
        f"${s['abandoned']:.2f} abandoned ({m['waste_share'] * 100:.0f}% waste)",
        f"Cost per shipped job (all spend / shipped): "
        f"{'$' + format(m['cost_per_shipped_usd'], '.2f') if m['cost_per_shipped_usd'] is not None else '-'}",
        f"Median minutes for shipped jobs: lead time {fmt(t['lead_time'])} = agents {fmt(t['agent_time'])} "
        f"+ review wait {fmt(t['review_wait'])} + post-merge {fmt(t['post_merge_verify'])}",
        f"Human interventions (stops): {m['interventions']}"
        + (f", {m['interventions_per_shipped']} per shipped job" if m['interventions_per_shipped'] else ""),
    ]
    return "\n".join(lines)
