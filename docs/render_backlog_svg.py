"""Render a factory's job board as a standalone SVG (light and dark mode).

    python docs/render_backlog_svg.py ~/.asf/<repo>/factory.db docs/images/board.svg "CycleSense"
"""

from __future__ import annotations

import json
import sqlite3
import sys
import textwrap
from html import escape

COLS = [
    ("Delivered on a branch", "green", {"done"}),
    ("In the pipeline", "blue", {"queued", "triaged", "implementing", "testing", "verifying", "reviewing",
                                 "committing", "delivering", "awaiting_approval", "merging", "observing"}),
    ("Parked for a human", "amber", {"needs_human", "failed", "paused", "rejected"}),
]
STYLE = """<style>
text{font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif}
.bg{fill:#FFFFFF}.card{fill:#F8F7F3;stroke:#D3D1C7}.sep{stroke:#D3D1C7}
.it{font-size:13px;font-weight:600;fill:#2C2C2A}.sub{font-size:12px;fill:#5F5E5A}.head{font-size:15px;font-weight:600;fill:#2C2C2A}
.green{fill:#3B6D11}.blue{fill:#185FA5}.amber{fill:#854F0B}.foot{font-size:12px;fill:#5F5E5A}
@media (prefers-color-scheme: dark){.bg{fill:#1F1F1E}.card{fill:#2C2C2A;stroke:#5F5E5A}.sep{stroke:#5F5E5A}
.it{fill:#F1EFE8}.sub{fill:#D3D1C7}.head{fill:#F1EFE8}.green{fill:#97C459}.blue{fill:#85B7EB}.amber{fill:#EF9F27}.foot{fill:#D3D1C7}}
</style>"""


def wrap(text: str, width: int, max_lines: int) -> list[str]:
    lines = textwrap.wrap(text, width)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: width - 1].rstrip(" ,.;") + "\u2026"
    return lines


def subtitle(job: dict, cost: float) -> str:
    d = job["data"]
    if job["state"] == "done":
        manual = " Needs a manual device check before merge." if d.get("manual_verification") else ""
        return f"Tier {job['tier']}, gates and reviews passed, ${cost:.2f}.{manual}"
    if job["state"] in ("needs_human", "failed", "paused"):
        reason = d.get("needs_human_reason") or d.get("error") or d.get("paused_reason") or ""
        return reason.split(":", 1)[-1].strip()
    return f"Now {job['state']}." if job["state"] not in ("queued", "triaged") else "Triaged, waiting to run."


def render(db_path: str, title: str) -> str:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    jobs = []
    for r in db.execute("SELECT j.*, w.payload FROM jobs j JOIN work_items w ON w.id = j.work_item_id ORDER BY j.created"):
        job = dict(r)
        job["data"] = json.loads(job["data"] or "{}")
        job["item"] = json.loads(job["payload"])
        job["cost"] = db.execute("SELECT COALESCE(SUM(cost_usd),0) FROM runs WHERE job_id=?", (job["id"],)).fetchone()[0]
        jobs.append(job)
    total = sum(j["cost"] for j in jobs)
    width, col_w, gap, x0, y0 = 960, 296, 16, 24, 60
    body, heights = [], []
    for i, (name, color, states) in enumerate(COLS):
        x = x0 + i * (col_w + gap)
        rows = [j for j in jobs if j["state"] in states]
        y = y0 + 44
        parts = [f'<text class="head {color}" x="{x + 14}" y="{y0 + 26}">{escape(name)} ({len(rows)})</text>']
        for j in rows:
            t_lines = wrap(j["item"]["title"], 38, 2)
            s_lines = wrap(subtitle(j, j["cost"]), 44, 3)
            parts.append(f'<line class="sep" x1="{x + 12}" y1="{y}" x2="{x + col_w - 12}" y2="{y}" stroke-width="0.8"/>')
            y += 20
            for line in t_lines:
                parts.append(f'<text class="it" x="{x + 14}" y="{y}">{escape(line)}</text>')
                y += 17
            for line in s_lines:
                parts.append(f'<text class="sub" x="{x + 14}" y="{y}">{escape(line)}</text>')
                y += 16
            y += 6
        heights.append(y - y0 + 8)
        body.append((x, parts))
    card_h = max(heights)
    height = y0 + card_h + 48
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">',
           f"<title>{escape(title)} backlog through the software factory</title>", STYLE,
           f'<rect class="bg" x="0" y="0" width="{width}" height="{height}"/>',
           f'<text class="head" x="{x0}" y="34">{escape(title)} backlog through the software factory</text>']
    for x, parts in body:
        out.append(f'<rect class="card" x="{x}" y="{y0}" width="{col_w}" height="{card_h}" rx="12" stroke-width="0.8"/>')
        out.extend(parts)
    out.append(f'<text class="foot" x="{x0}" y="{height - 18}">{len(jobs)} work items from the beads tracker. '
               f'Total model spend ${total:.2f}. Nothing merged: shadow mode.</text>')
    out.append("</svg>")
    return "\n".join(out)


if __name__ == "__main__":
    db, dest = sys.argv[1], sys.argv[2]
    title = sys.argv[3] if len(sys.argv) > 3 else "Repository"
    open(dest, "w").write(render(db, title))
    print(f"wrote {dest}")
