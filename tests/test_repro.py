"""Browser reproductions with a scripted reproducer: the flow around the agent, not the agent.

The app is a tiny HTTP server whose /value says "broken" until the implementer fixes it. The
"browser" is scripted; the e2e tests it writes are real pytest files run against the real app,
so their failure types (assertion, crash, pass) are the real thing.
"""

import json
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from asf import repro as repro_mod
from asf.adapters.models.scripted import ScriptedProvider
from asf.apprun import AppError, classify, running_app
from asf.config import ConfigError
from asf.gates import GateRunner
from asf.models import WorkItem
from asf.orchestrator import REPRO_EDIT, Factory
from asf.util import ExecResult
from conftest import INVOICE_FILES, PY, invoice_config, load_cfg, make_repo, sh
from test_pipeline import APPROVE, TESTER

SERVER = """\
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

VALUE = "broken"


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (VALUE if self.path == "/value" else "ok").encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"""
E2E_CONFTEST = ('import os\n\ncollect_ignore_glob = [] if os.environ.get("ASF_APP_URL") else ["test_*.py"]\n')
APP_FILES = {**INVOICE_FILES, "app/server.py": SERVER, "tests/e2e/conftest.py": E2E_CONFTEST}

TEST_OK = """\
import os
import urllib.request


def test_value_is_fixed():
    got = urllib.request.urlopen(os.environ["ASF_APP_URL"] + "/value").read().decode()
    assert got == "fixed", f"value was {got!r}, expected 'fixed'"
"""
TEST_PASSES_ON_BASE = TEST_OK.replace('got == "fixed"', 'got in ("fixed", "broken")')
TEST_CRASHES = TEST_OK.replace('    got = urllib', '    raise RuntimeError("selector not found")\n    got = urllib')

PLAN = {"applicable": True, "kind": "bug", "path": "/value",
        "steps": ["Open /value", "Read the text"], "expected": "The page says fixed",
        "measure": ["value: the page text (expected: fixed, bug: broken)"], "covers": ["app/**"]}
TRIAGE = {"summary": "value is broken", "acceptance_criteria": ["/value says fixed"], "likely_paths": ["app/server.py"],
          "tier_guess": 2, "automatable": True, "reason_if_not": "", "clarification_needed": "",
          "manual_verification": [], "repro": PLAN}

FIX = [[{"name": "edit_file", "input": {"path": "app/server.py", "old_text": 'VALUE = "broken"',
                                        "new_text": 'VALUE = "fixed"'}}],
       [{"name": "run_check", "input": {"kind": "e2e", "args": "tests/e2e/test_value_page_says_broken.py"}}],
       [{"name": "finish", "input": {"summary": "Fixed the value."}}]]
NO_FIX = [[{"name": "write_file", "input": {"path": "app/notes.py", "content": "X = 1\n"}}],
          [{"name": "finish", "input": {"summary": "Tried."}}]]


def shows(value="broken", test=TEST_OK):
    return {"reproduced": True, "measurements": [{"name": "value", "value": value, "expected": "fixed"}],
            "notes": "the page says broken", "test_source": test}


GONE = {"reproduced": False, "measurements": [{"name": "value", "value": "fixed", "expected": "fixed"}],
        "notes": "the page says fixed"}


def app_config(**overrides):
    base = {
        "packages": [{"name": "invoice", "root": ".", "triggers": ["**"], "commands": {
            "test": f"{PY} -m pytest -q -p no:cacheprovider {{args}}",
            "e2e": f"{PY} -m pytest -q -p no:cacheprovider {{args}}",
            "repro": "tests/repro"}}],
        "app": {"start": f"{PY} app/server.py {{port}}", "health": "/", "start_timeout_s": 20},
    }
    base.update(overrides)
    return invoice_config(**base)


@pytest.fixture
def app_repo(tmp_path):
    return make_repo(tmp_path / "invoice", APP_FILES)


def make(repo, state, store, reproducer, implementer=None, triage=TRIAGE, **cfg):
    prov = ScriptedProvider({"scripts": {"implementer": implementer or [FIX], "tester": [TESTER, TESTER],
                                         "reviewer": [APPROVE, APPROVE], "security": [APPROVE, APPROVE]},
                             "structured": {"triage": triage}, "reproducer": reproducer})
    return Factory(repo, load_cfg(app_config(**cfg)), state, store, provider=prov), prov


def bug(title="Value page says broken", kind="bug"):
    return WorkItem("local", title[:12], title, body="GET /value shows 'broken'", kind=kind)


# ------------------------------------------------------------------------- the flow --

def test_bug_is_reproduced_committed_then_confirmed_gone(app_repo, state, store):
    f, prov = make(app_repo, state, store, [shows(), GONE])
    job_id, _ = f.submit(bug())
    assert f.run_job(job_id) == "awaiting_review"
    d = store.job(job_id)["data"]
    r = d["repro"]
    assert r["md"] == "tests/repro/value-page-says-broken.md"
    assert r["test"] == "tests/e2e/test_value_page_says_broken.py"
    assert r["base"]["reproduced"] and r["base"]["verdict"] == "bug shows"
    assert [t["kind"] for t in r["test_on_base"]] == ["assertion"]
    assert r["after"]["e2e"]["status"] == "pass" and r["after"]["browser"]["verdict"] == "bug gone"
    assert ("e2e", "pass") in {(g["name"], g["status"]) for g in d["gates"]}
    # The reproducer ran on base (writing the test) and after the fix (checking only).
    assert [m for role, m in prov.log if role == "reproducer"] == ["reproduce", "confirm"]
    base_req, after_req = prov.repro_requests
    assert base_req.want_test and not after_req.want_test and base_req.url.startswith("http://127.0.0.1:")
    # Two commits on the branch: the reproduction (before the fix), then the fix on top of it.
    branch = d["branch"]
    log = sh(app_repo, "git", "log", "--format=%s", f"main..{branch}").splitlines()
    assert log[-1] == "Reproduce: Value page says broken" and len(log) == 2
    assert sh(app_repo, "git", "show", "--name-only", "--format=", r["commit"]).split() == \
        ["tests/e2e/test_value_page_says_broken.py", "tests/repro/value-page-says-broken.md"]
    md = sh(app_repo, "git", "show", f"{branch}:tests/repro/value-page-says-broken.md")
    spec = repro_mod.parse(md, r["md"])
    assert spec.item == "local:Value page s" and spec.covers == ["app/**"] and spec.url_path == "/value"
    assert "1. Open /value" in md and "## Measure" in md and "value: the page text" in md
    assert sh(app_repo, "git", "show", f"{branch}:tests/e2e/test_value_page_says_broken.py") == TEST_OK
    # The implementer was told about the reproduction and that it cannot change it.
    assert any("tests/e2e/test_value_page_says_broken.py" in e["payload"].get("args", {}).get("args", "")
               for e in store.events(job_id) if e["type"] == "tool.call")


def test_bug_not_reproduced_on_base_asks_the_requester(app_repo, state, store):
    f, prov = make(app_repo, state, store, [GONE])
    job_id, _ = f.submit(bug())
    assert f.run_job(job_id) == "needs_human"
    d = store.job(job_id)["data"]
    assert d["needs_human_reason"].startswith("bug not understood")
    assert "(measured value: fixed)" in d["needs_human_reason"]
    assert not (d["repro"] or {}).get("commit")
    assert [r for r, _ in prov.log] == ["triage", "reproducer"]  # no implementer spend
    # An answer re-triages (with the answer) and reproduces again.
    prov.repro_answers.append(shows())
    prov.repro_answers.append(GONE)
    prov.structured_answers["triage"] = TRIAGE
    assert "continuing from queued" in f.answer(job_id, "Open /value with the cache off", by="owner")
    assert f.run_job(job_id) == "awaiting_review"
    assert "Open /value with the cache off" in prov.repro_requests[1].answers[0]["answer"]


def test_feature_steps_must_show_the_behaviour_missing(app_repo, state, store):
    f, _ = make(app_repo, state, store, [GONE], triage={**TRIAGE, "repro": {**PLAN, "kind": "feature"}})
    job_id, _ = f.submit(bug("Show the fixed value", kind="feature"))
    assert f.run_job(job_id) == "needs_human"
    assert store.job(job_id)["data"]["needs_human_reason"].startswith(
        "feature not understood: the reproducer followed the steps on the current code in a browser and "
        "the behaviour is already there")


def test_test_that_passes_on_base_is_regenerated_once(app_repo, state, store):
    f, prov = make(app_repo, state, store, [shows(test=TEST_PASSES_ON_BASE), {"reproduced": True, "test_source": TEST_OK},
                                            GONE])
    job_id, _ = f.submit(bug())
    assert f.run_job(job_id) == "awaiting_review"
    r = store.job(job_id)["data"]["repro"]
    assert [t["kind"] for t in r["test_on_base"]] == ["pass", "assertion"]
    regen = prov.repro_requests[1]
    assert regen.mode == "regenerate" and regen.previous_test == TEST_PASSES_ON_BASE
    assert "Failure kind: pass" in regen.test_output
    assert sh(app_repo, "git", "show", f"{r['commit']}:{r['test']}") == TEST_OK


def test_test_failing_by_crash_twice_stops_for_a_human(app_repo, state, store):
    f, prov = make(app_repo, state, store, [shows(test=TEST_CRASHES), {"reproduced": True, "test_source": TEST_CRASHES}])
    job_id, _ = f.submit(bug())
    assert f.run_job(job_id) == "needs_human"
    d = store.job(job_id)["data"]
    assert d["needs_human_reason"].startswith("repro test unusable")
    assert "crash" in d["needs_human_reason"] and "RuntimeError" in d["needs_human_reason"]
    assert d["failed_state"] == "reproducing" and not d["repro"].get("commit")
    assert "implementer" not in [r for r, _ in prov.log]


def test_e2e_passes_but_browser_still_shows_bug_is_a_fix_round_then_human(app_repo, state, store):
    # The test passes after the fix, but a real browser still sees the bug: the fix satisfies the
    # test without fixing the bug. One fix round with the evidence, then a human.
    f, prov = make(app_repo, state, store, [shows(), shows(), shows()], implementer=[FIX, NO_FIX])
    job_id, _ = f.submit(bug())
    assert f.run_job(job_id) == "needs_human"
    d = store.job(job_id)["data"]
    assert d["confirm_rounds"] == 1
    assert d["needs_human_reason"].startswith("fix not confirmed")
    assert "passes, but in a real browser the bug still shows (value: broken)" in d["needs_human_reason"]
    assert [r for r, _ in prov.log].count("implementer") == 2
    events = [e for e in store.events(job_id) if e["type"] == "repro.not_confirmed"]
    assert len(events) == 2


def test_browser_gone_but_test_fails_is_a_defect_with_evidence(app_repo, state, store):
    f, prov = make(app_repo, state, store, [shows(), GONE, GONE], implementer=[NO_FIX, FIX])
    job_id, _ = f.submit(bug())
    # e2e is not among the gates here, so the disagreement reaches the confirm step.
    for t in range(4):
        f.cfg.raw["gates"][f"tier_{t}"]["deterministic"] = [g for g in f.cfg.raw["gates"][f"tier_{t}"]["deterministic"]
                                                            if g != "e2e"]
    assert f.run_job(job_id) == "awaiting_review"
    d = store.job(job_id)["data"]
    assert d["confirm_rounds"] == 1
    fb = [e for e in store.events(job_id) if e["type"] == "repro.not_confirmed"][0]["payload"]["problems"][0]
    assert "the bug is gone (value: fixed), but tests/e2e/test_value_page_says_broken.py still fails (assertion)" in fb


def test_implementer_cannot_edit_the_reproduction(app_repo, state, store):
    tamper = [[{"name": "write_file", "input": {"path": "tests/e2e/test_value_page_says_broken.py",
                                                "content": "def test_x():\n    pass\n"}},
               {"name": "write_file", "input": {"path": "tests/repro/value-page-says-broken.md", "content": "x"}}]] + FIX
    f, _ = make(app_repo, state, store, [shows(), GONE], implementer=[tamper])
    job_id, _ = f.submit(bug())
    assert f.run_job(job_id) == "awaiting_review"
    denials = [e["payload"] for e in store.events(job_id)
               if e["type"] == "tool.call" and e["payload"]["decision"] == "deny"]
    assert len(denials) == 2 and all("this job's reproduction" in d["reason"] for d in denials)


def test_test_integrity_gate_enforces_the_committed_reproduction(app_repo, state, store):
    f, _ = make(app_repo, state, store, [shows()], implementer=[[[{"name": "finish", "input": {"summary": "x"}}]]])
    job_id, _ = f.submit(bug())
    f.run_job(job_id, stop_at="implementing")
    job = store.job(job_id)
    sb = f._sandbox(job)
    (sb.root / job["data"]["repro"]["test"]).write_text("def test_nothing():\n    pass\n")
    runner = GateRunner(f.cfg, sb, 2, state, protected=f._repro_contents(job, sb))
    [res] = runner.run(["test_integrity"])
    assert res.status == "fail" and "modified the reproduction tests/e2e/test_value_page_says_broken.py" in res.detail


def test_selector_change_needs_owner_approval(app_repo, state, store):
    blocked = [[{"name": "finish", "input": {"summary": "x", "blocked_reason":
                                             f"{REPRO_EDIT}: the page moves to /v2/value"}}]]
    edit = [[{"name": "write_file", "input": {"path": "tests/e2e/test_value_page_says_broken.py",
                                              "content": TEST_OK.replace("got ==", "got.strip() ==")}}]] + FIX
    f, prov = make(app_repo, state, store, [shows(), GONE], implementer=[blocked, edit])
    job_id, _ = f.submit(bug())
    assert f.run_job(job_id) == "needs_human"
    d = store.job(job_id)["data"]
    assert d["needs_human_reason"] == f"{REPRO_EDIT}: the page moves to /v2/value"
    assert "allow_repro_edit" not in d  # nothing approved yet
    assert f.allow_repro_edit(job_id, by="lead") == "implementing"
    assert f.run_job(job_id) == "awaiting_review"
    r = store.job(job_id)["data"]["repro"]
    assert r["edit_approved"]["by"] == "lead" and r["protected"] == [r["md"]]
    # The edited test still had to fail by its assertion on the code before the fix.
    assert r["after"]["test_on_base"]["kind"] == "assertion"
    assert r["after"]["e2e"]["status"] == "pass"


def test_change_touching_a_covered_path_reruns_that_reproduction(app_repo, state, store):
    # A reproduction committed earlier covers app/**; a later task that changes app/ must keep it gone.
    md = repro_mod.render("Value page says broken", "local:1", "bug", ["app/**"], "/value", ["Open /value"],
                          "says fixed", ["value"])
    (app_repo / "tests/repro").mkdir(parents=True)
    (app_repo / "tests/repro/value.md").write_text(md)
    sh(app_repo, "git", "add", "-A")
    sh(app_repo, "git", "commit", "-qm", "repro")
    plain = {**TRIAGE, "repro": {**PLAN, "applicable": False, "steps": []}}
    f, prov = make(app_repo, state, store, [shows(), shows()], triage=plain,
                   implementer=[[[{"name": "write_file", "input": {"path": "app/extra.py", "content": "Y = 2\n"}},
                                  {"name": "write_file", "input": {"path": "tests/test_extra.py",
                                                                   "content": "def test_y():\n    assert 2 == 2\n"}},
                                  {"name": "finish", "input": {"summary": "x"}}]],
                                [[{"name": "finish", "input": {"summary": "again"}}]]])
    job_id, _ = f.submit(WorkItem("local", "t1", "Add extra", kind="task"))
    assert f.run_job(job_id) == "needs_human"
    d = store.job(job_id)["data"]
    assert [c["md"] for c in d["repro_checks"]] == ["tests/repro/value.md"]
    # Still broken on base, so the covered reproduction "shows the problem": a regression to fix.
    assert "Regression: tests/repro/value.md" in d["needs_human_reason"]
    assert [m for r, m in prov.log if r == "reproducer"] == ["confirm", "confirm"]
    assert d["confirm_rounds"] == 1


def test_change_outside_covered_paths_runs_no_reproduction(app_repo, state, store):
    plain = {**TRIAGE, "repro": {**PLAN, "applicable": False, "steps": []}}
    f, prov = make(app_repo, state, store, [], triage=plain, implementer=[[[
        {"name": "write_file", "input": {"path": "docs/x.md", "content": "hi\n"}},
        {"name": "finish", "input": {"summary": "docs"}}]]])
    job_id, _ = f.submit(WorkItem("local", "d1", "Docs", kind="task"))
    assert f.run_job(job_id) == "awaiting_review"
    assert "reproducer" not in [r for r, _ in prov.log]
    assert "repro_checks" not in store.job(job_id)["data"]


def test_no_app_configured_skips_reproduction(app_repo, state, store):
    f, prov = make(app_repo, state, store, [], app={"start": ""})
    job_id, _ = f.submit(bug())
    f.run_job(job_id, stop_at="implementing")
    assert store.job(job_id)["data"]["repro_plan"] is None
    assert any(e["type"] == "repro.skipped" and "no app" in e["payload"]["reason"] for e in store.events(job_id))


def test_post_merge_runs_the_e2e_suite_against_the_merged_app(app_repo, state, store):
    (app_repo / "tests/e2e/test_ok.py").write_text(TEST_OK)  # main is still broken
    sh(app_repo, "git", "add", "-A")
    sh(app_repo, "git", "commit", "-qm", "e2e")
    f, _ = make(app_repo, state, store, [])
    failure = f._post_merge_checks()
    assert failure.startswith("e2e suite (invoice) failed") and "value was 'broken'" in failure


def test_repro_suite_runs_every_reproduction_on_the_default_branch(app_repo, state, store):
    md = repro_mod.render("Value page says broken", "local:1", "bug", ["app/**"], "/value", ["Open /value"],
                          "says fixed", ["value"])
    (app_repo / "tests/repro").mkdir(parents=True)
    (app_repo / "tests/repro/value.md").write_text(md)
    (app_repo / "tests/repro/README.md").write_text("not a reproduction")
    sh(app_repo, "git", "add", "-A")
    sh(app_repo, "git", "commit", "-qm", "repro")
    f, prov = make(app_repo, state, store, [shows()])
    [res] = f.repro_suite()
    assert res["md"] == "tests/repro/value.md" and res["reproduced"] and res["verdict"] == "bug shows"
    assert prov.repro_requests[0].mode == "confirm" and prov.repro_requests[0].spec.covers == ["app/**"]
    assert store.runs("repro-suite")[0]["role"] == "reproducer"


# ------------------------------------------------------------------------- units --

def res(code, out):
    return ExecResult(code, out, "", 1.0)


@pytest.mark.parametrize("out,code,kind", [
    ("F\nFAILED tests/e2e/test_x.py::test_x - AssertionError: value was 0\n1 failed", 1, "assertion"),
    ("F\nFAILED tests/e2e/test_x.py::test_x - assert 0 > 700\n1 failed", 1, "assertion"),
    ("FAILED tests/e2e/test_x.py::test_x - AssertionError: Locator expected to have text 'x'", 1, "assertion"),
    ("FAILED t.py::t - playwright._impl._errors.TimeoutError: Locator.click: Timeout 30000ms exceeded.\n"
     "  - waiting for get_by_role(\"button\", name=\"Diff\")", 1, "selector"),
    ("FAILED t.py::t - playwright._impl._errors.TimeoutError: Page.goto: Timeout 30000ms exceeded.", 1, "timeout"),
    ("FAILED t.py::t - RuntimeError: boom", 1, "crash"),
    ("ERROR t.py::t - fixture 'page' not found", 1, "crash"),
    ("no tests ran in 0.01s", 5, "no_tests"),
    ("1 passed", 0, "pass"),
])
def test_classify_failure_types(out, code, kind):
    assert classify(res(code, out)).kind == kind


def test_mixed_failures_are_judged_by_the_worst(tmp_path):
    out = "FAILED a.py::a - AssertionError: x\nFAILED b.py::b - RuntimeError: y"
    assert classify(res(1, out)).kind == "crash"


def test_config_accepts_app_and_browser_commands():
    cfg = load_cfg(app_config())
    assert cfg.app["start"].endswith("{port}") and cfg.repro_dirs()[0][1] == "tests/repro"
    assert "e2e" in cfg.gates_for(2)["deterministic"]
    with pytest.raises(ConfigError, match="app.port"):
        load_cfg(app_config(app={"start": "x", "port": -1}))
    with pytest.raises(ConfigError, match="unknown command kind"):
        load_cfg(app_config(packages=[{"name": "p", "commands": {"browser": "x"}}]))


def test_running_app_reports_an_app_that_never_gets_healthy(app_repo):
    cfg = load_cfg(app_config(app={"start": f"{PY} -c 'import sys; print(\"boom\"); sys.exit(3)'",
                                   "start_timeout_s": 5}))
    with pytest.raises(AppError, match="exited with code 3"):
        with running_app(cfg, app_repo):
            pass


def test_running_app_stops_the_app(app_repo):
    import urllib.request
    cfg = load_cfg(app_config())
    with running_app(cfg, app_repo) as url:
        assert urllib.request.urlopen(url + "/value").read() == b"broken"
    with pytest.raises(OSError):
        urllib.request.urlopen(url + "/value", timeout=2)


def test_render_and_parse_round_trip():
    text = repro_mod.render("Diff box jumps", "github_issues:6", "bug", ["asf/ui_static/**"], "/#p/demo/JOB-demo01",
                            ["1. Open it", "Click Diff"], "It stays", ["top: scrollTop (expected: 800)"],
                            test="tests/e2e/test_diff_box_jumps.py")
    spec = repro_mod.parse(text, "tests/repro/diff-box-jumps.md")
    assert (spec.title, spec.item, spec.kind, spec.covers, spec.url_path, spec.test) == (
        "Diff box jumps", "github_issues:6", "bug", ["asf/ui_static/**"], "/#p/demo/JOB-demo01",
        "tests/e2e/test_diff_box_jumps.py")
    assert "1. Open it\n2. Click Diff" in text and spec.slug == "diff-box-jumps"


# ------------------------------------------------------------- the real reproducer --

FAKE_CLAUDE = textwrap.dedent('''\
    #!{py}
    import json, os, sys
    open(os.environ["FAKE_LOG"], "w").write(json.dumps({{"args": sys.argv[1:], "prompt": sys.stdin.read(),
        "cwd": os.getcwd(), "key": os.environ.get("ANTHROPIC_API_KEY")}}))
    open("measured.png", "wb").write(b"png")
    print(json.dumps({{"total_cost_usd": 0.05, "num_turns": 7, "usage": {{"input_tokens": 9}},
        "modelUsage": {{"claude-sonnet-5-5": {{}}}},
        "structured_output": {{"reproduced": True, "measurements": [], "notes": "n",
                               "screenshots": ["measured.png"], "test_source": "def test(): assert False"}}}}))
''')


def test_claude_reproducer_has_a_browser_and_nothing_else(tmp_path, monkeypatch):
    exe = tmp_path / "claude"
    exe.write_text(FAKE_CLAUDE.format(py=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "log.json"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    cfg = load_cfg(app_config(repro={"binary": str(exe), "model": "claude-sonnet-5-5"}))
    spec = repro_mod.parse(repro_mod.render("T", "x:1", "bug", [], "/value", ["Open"], "ok", ["v"]), "tests/repro/t.md")
    out_dir = tmp_path / "evidence" / "base-1"
    res, turn = repro_mod.ClaudeReproducer(cfg).reproduce(repro_mod.ReproRequest(
        spec=spec, url="http://127.0.0.1:5555", mode="reproduce", out_dir=out_dir, test_path="tests/e2e/test_t.py"))
    call = json.loads(log.read_text())
    args = call["args"]
    assert args[args.index("--tools") + 1] == ""                       # no built-in tools at all
    assert "--strict-mcp-config" in args and args[args.index("--permission-mode") + 1] == "dontAsk"
    denied = args[args.index("--disallowedTools") + 1]
    assert "mcp__browser__browser_run_code_unsafe" in denied and "mcp__browser__browser_file_upload" in denied
    allowed = args[args.index("--allowedTools") + 1].split(",")
    assert all(a.startswith("mcp__browser__browser_") for a in allowed)
    assert not any(t in allowed for t in denied.split(","))
    assert json.loads(args[args.index("--json-schema") + 1]) == repro_mod.REPRO_SCHEMA
    assert call["key"] is None and Path(call["cwd"]).resolve() == out_dir.resolve()
    assert '<repro file="tests/repro/t.md">' in call["prompt"] and "http://127.0.0.1:5555/value" in call["prompt"]
    assert res["reproduced"] and turn.cost_usd == 0.05 and turn.model == "claude:claude-sonnet-5-5"
    assert repro_mod.shots(out_dir, tmp_path / "evidence") == ["base-1/measured.png"]


def test_mcp_config_is_the_playwright_server_only_pinned_to_the_app(tmp_path):
    cfg = load_cfg(app_config())
    conf = repro_mod.ClaudeReproducer(cfg).mcp_config("http://127.0.0.1:5555", tmp_path)
    [(name, server)] = conf["mcpServers"].items()
    assert name == "browser" and server["command"] == "npx"
    a = server["args"]
    assert "@playwright/mcp@0.0.85" in a and "--headless" in a and "--isolated" in a
    assert a[a.index("--allowed-origins") + 1] == "http://127.0.0.1:5555"
    assert a[a.index("--output-dir") + 1] == str(tmp_path)


def test_console_shows_the_evidence(app_repo, state, store):
    import urllib.error
    import urllib.request

    from test_ui import KEY, get, start
    f, _ = make(app_repo, state, store, [shows(), GONE])
    job_id, _ = f.submit(bug())
    assert f.run_job(job_id) == "awaiting_review"
    shot = state / "evidence" / job_id / "base-1" / "measured.png"
    shot.write_bytes(b"\x89PNG fake")
    console, server, base = start(f)
    try:
        detail = get(f"{base}/api/p/{KEY}/jobs/{job_id}")
        r = detail["data"]["repro"]
        assert r["base"]["verdict"] == "bug shows" and r["after"]["browser"]["verdict"] == "bug gone"
        assert r["after"]["e2e"]["status"] == "pass" and r["test_on_base"][0]["kind"] == "assertion"
        img = urllib.request.urlopen(f"{base}/api/p/{KEY}/jobs/{job_id}/evidence/base-1/measured.png")
        assert img.headers["Content-Type"] == "image/png" and img.read() == b"\x89PNG fake"
        for bad in ("base-1/../../../factory.db", "base-1/app.log"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(f"{base}/api/p/{KEY}/jobs/{job_id}/evidence/{bad}")
            assert e.value.code == 404
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "function reproEvidence" in page and "allow_repro_edit" in page
    finally:
        server.shutdown()
