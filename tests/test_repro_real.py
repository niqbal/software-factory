"""Opt-in: the whole reproduction flow on a real bug, with a real browser agent.

    ASF_REAL_REPRO=1 .venv/bin/python -m pytest -q -s tests/test_repro_real.py

The bug: in the console, the diff box jumps back to the top on the 4 s refresh. The factory runs
on a copy of this repository with its own factory.yaml (`app:` starts `python -m asf.demo`).
Triage, implementer, tester and reviewer are scripted (the implementer applies FIX below); the
reproducer is the real one: `claude -p` with built-in tools off, driving Chromium through
@playwright/mcp. It must reproduce the bug on base and write a Playwright test that fails there
by its assertion; after the fix the test must pass and the reproducer must see the bug gone.

Needs: the `claude` CLI signed in, `npx`, and pytest-playwright with Chromium in .venv
(`pip install -e '.[e2e]' && playwright install chromium`). Costs a few reproducer runs of model
time. ASF_REAL_REPRO_KEEP=<dir> copies the generated reproduction and test there.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from asf import config as config_mod
from asf.adapters.models.scripted import ScriptedProvider
from asf.models import WorkItem
from asf.orchestrator import Factory
from asf.repro import ClaudeReproducer
from asf.store import Store
from conftest import sh

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not os.environ.get("ASF_REAL_REPRO"), reason="opt-in: set ASF_REAL_REPRO=1")

# The fix, as the implementer applies it (old text, new text) to asf/ui_static/index.html.
FIX = [
    ("function renderDetail() {\n  if (!D) return;\n",
     "function renderDetail() {\n  if (!D) return;\n"
     "  // The periodic refresh redraws the panel: on the same job and tab, keep the reader's place in the diff.\n"
     "  const same = renderDetail.tab === tab && renderDetail.job === D.id;\n"
     "  const keep = same ? [...$('detail').querySelectorAll('.diff')].map(e => e.scrollTop) : [];\n"
     "  renderDetail.tab = tab; renderDetail.job = D.id;\n"),
    ("    <div class=\"body\">${body}</div>`;\n}\n",
     "    <div class=\"body\">${body}</div>`;\n"
     "  $('detail').querySelectorAll('.diff').forEach((e, i) => { if (keep[i]) e.scrollTop = keep[i]; });\n}\n"),
]
PLAN = {
    "applicable": True, "kind": "bug", "path": "/#p/demo/JOB-demo01",
    "steps": [
        "Open the start page. Wait until the job \"Double every handler\" is shown with its tabs "
        "(Overview, Diff, Report, Timeline).",
        "Click the \"Diff\" tab.",
        "Scroll the diff box (the scrollable box with class `diff` that holds the patch lines) down by "
        "setting its scrollTop to 800.",
        "Read its scrollTop: it is 800.",
        "Wait 6 seconds without touching the page. The console refreshes the open job every 4 seconds.",
        "Read the diff box's scrollTop again.",
    ],
    "expected": "The diff box stays where it was scrolled to: scrollTop is still about 800 after the refresh.",
    "measure": ["diff_scroll_top_before: the diff box's scrollTop right after scrolling (expected: 800)",
                "diff_scroll_top_after: the diff box's scrollTop after the 6 second wait "
                "(expected: about 800; bug: 0)"],
    "covers": ["asf/ui_static/**"],
}
TRIAGE = {"summary": "Keep the diff box's scroll position across the console's periodic refresh",
          "acceptance_criteria": ["The diff box keeps its scroll position when the job view refreshes"],
          "likely_paths": ["asf/ui_static/index.html"], "tier_guess": 1, "automatable": True,
          "reason_if_not": "", "clarification_needed": "", "manual_verification": [], "repro": PLAN}
IMPLEMENT = [[{"name": "edit_file", "input": {"path": "asf/ui_static/index.html", "old_text": old, "new_text": new}}
              for old, new in FIX],
             [{"name": "finish", "input": {"summary": "Keep the diff box's scroll position across refreshes."}}]]
DONE = [[{"name": "finish", "input": {"summary": "Checked.", "tests_pass": True}}]]
APPROVE = [[{"name": "submit_review", "input": {"verdict": "approve", "summary": "Looks right."}}]]


def copy_repo(dst: Path) -> Path:
    """This checkout's files (committed or not) as a fresh repository, with the bug in place."""
    files = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-co", "--exclude-standard"],
                           capture_output=True, text=True, check=True).stdout.split("\n")
    for rel in filter(None, files):
        src = ROOT / rel
        if src.is_file():
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst / rel)
    page = dst / "asf/ui_static/index.html"
    text = page.read_text()
    for old, new in FIX:  # the fix may already be in this checkout: take it out again
        text = text.replace(new, old)
    assert all(old in text for old, _ in FIX), "index.html changed; update FIX in this test"
    page.write_text(text)
    for p in (dst / "tests/repro").glob("*.md"):
        if p.name != "README.md":
            p.unlink()
    for p in (dst / "tests/e2e").glob("test_*.py"):
        p.unlink()
    (dst / ".venv").symlink_to(ROOT / ".venv")
    sh(dst, "git", "init", "-q", "-b", "main")
    sh(dst, "git", "add", "-A")
    sh(dst, "git", "commit", "-q", "-m", "base: the diff box jumps to the top on refresh")
    return dst


def test_console_diff_scroll_bug_end_to_end(tmp_path):
    for tool in ("claude", "npx"):
        if not shutil.which(tool) and not (Path.home() / ".local/bin" / tool).exists():
            pytest.skip(f"{tool} not found")
    if not (ROOT / ".venv/bin/python").exists():
        pytest.skip("no .venv with pytest-playwright")
    repo = copy_repo(tmp_path / "software-factory")
    state = tmp_path / "state"
    raw = config_mod.load(repo / "factory.yaml").raw
    raw["packages"][0]["commands"]["test"] = ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_repro.py -k classify {args}"
    raw["execution"].update({"clone_dirs": [], "setup": []})
    raw["intake"], raw["observe"], raw["notifications"] = {}, {"provider": "none"}, {"providers": []}
    raw["budgets"]["tier_1"] = {"usd": 50, "minutes": 60}
    cfg = config_mod.Config(raw=raw)
    prov = ScriptedProvider({"scripts": {"implementer": [IMPLEMENT], "tester": [DONE], "reviewer": [APPROVE],
                                         "security": [APPROVE]}, "structured": {"triage": TRIAGE}})
    f = Factory(repo, cfg, state, Store(state / "factory.db"), provider=prov, reproducer=ClaudeReproducer(cfg),
                echo=print)
    job_id, _ = f.submit(WorkItem("github_issues", "6", "The diff box jumps to the top on the 4 s refresh",
                                  body="In the console, scroll the diff of a job, and a few seconds later it is back "
                                       "at the top.", kind="bug"))
    final = f.run_job(job_id)
    d = f.store.job(job_id)["data"]
    r = d.get("repro") or {}
    print("\nreason:", d.get("needs_human_reason") or d.get("error"))
    for phase in ("base", "regenerated"):
        if r.get(phase):
            print(phase, r[phase]["verdict"], r[phase]["measurements"], r[phase]["notes"][:300])
    print("test on base:", [(t["kind"], t["detail"][:200]) for t in r.get("test_on_base", [])])
    if r.get("after"):
        print("after:", r["after"]["e2e"]["status"], r["after"]["e2e"]["detail"][:200], "|",
              r["after"]["browser"]["verdict"], r["after"]["browser"]["measurements"])
    keep = os.environ.get("ASF_REAL_REPRO_KEEP")
    if keep and r.get("commit"):
        for rel in (r["md"], r["test"]):
            out = Path(keep) / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(sh(repo, "git", "show", f"{r['commit']}:{rel}"))
    assert final == "awaiting_review", d.get("needs_human_reason") or d.get("error")
    assert r["base"]["reproduced"] and r["test_on_base"][-1]["kind"] == "assertion"
    assert r["after"]["e2e"]["status"] == "pass" and not r["after"]["browser"]["reproduced"]
    assert r["base"]["screenshots"] and (state / "evidence" / job_id / r["base"]["screenshots"][0]).exists()
    assert ("e2e", "pass") in {(g["name"], g["status"]) for g in d["gates"]}
    log = sh(repo, "git", "log", "--format=%s", f"main..{d['branch']}").splitlines()
    assert log[-1].startswith("Reproduce: ") and len(log) == 2
