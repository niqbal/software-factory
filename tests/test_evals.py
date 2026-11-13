"""The eval harness (asf eval): case format, mining, the runner, the baseline and the report.

Everything runs against a synthetic repository with the scripted model provider and a fake
`claude` binary: no network, no spend. The archive is a temporary folder.
"""

import json
import shlex
import stat
import textwrap
from pathlib import Path

import pytest
import yaml

from asf import cli, limits
from asf.evals import mine as mine_mod
from asf.evals import report as report_mod
from asf.evals import runner, score, variants, workspace
from asf.evals.cases import EvalError, EvalSet, guard_archive, load_case
from conftest import PY, invoice_config, make_repo, sh

EXAMPLE = Path(__file__).parent / "evals" / "example_case.yaml"
BUG = "    return int(cents * (100 - percent) / 100)\n"
FIX = "    return (cents * (100 - percent) + 50) // 100\n"
HIDDEN = """from invoice.totals import apply_discount


def test_half_cent_rounds_up():
    assert apply_discount(999, 50) == 500


def test_one_cent_half_off():
    assert apply_discount(1, 50) == 1


def test_whole():
    assert apply_discount(1000, 10) == 900
"""
TRIAGE = {"summary": "round discounts", "acceptance_criteria": ["apply_discount rounds half up"],
          "likely_paths": ["invoice/totals.py"], "tier_guess": 2, "automatable": True,
          "reason_if_not": "", "clarification_needed": "", "manual_verification": []}
APPROVE = [[{"name": "submit_review", "input": {"verdict": "approve", "summary": "ok"}}]]
TESTER = [[{"name": "run_check", "input": {"kind": "test"}}],
          [{"name": "finish", "input": {"summary": "fine", "tests_pass": True, "tests_added": []}}]]


def implementer(new_code: str) -> list:
    return [
        [{"name": "read_file", "input": {"path": "invoice/totals.py"}}],
        [{"name": "edit_file", "input": {"path": "invoice/totals.py", "old_text": BUG, "new_text": new_code}},
         {"name": "write_file", "input": {"path": "tests/test_discount.py", "content":
             "from invoice.totals import apply_discount\n\n\ndef test_999():\n"
             "    assert apply_discount(999, 50) == 500\n"}}],
        [{"name": "finish", "input": {"summary": "Round discounts.", "acceptance_met": [],
                                      "tests_added": ["tests/test_discount.py"]}}],
    ]


# Passes its own test and the gates, but only special-cases the example in the ticket.
GAMING = "    if cents == 999 and percent == 50:\n        return 500\n" + BUG


def scripted_variant(impl: list) -> dict:
    return {"description": "scripted", "replace": ["models"], "overlay": {"models": {
        "provider": "scripted", "structured": {"triage": TRIAGE},
        "scripts": {"implementer": [impl], "tester": [TESTER], "reviewer": [APPROVE], "security": [APPROVE]}}}}


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "asf-home"
    monkeypatch.setenv("ASF_HOME", str(h))
    return h


@pytest.fixture
def product(tmp_path):
    """The invoice repo with a base commit and a reference commit that fixes the bug and adds tests."""
    from conftest import INVOICE_FILES
    repo = make_repo(tmp_path / "invoice", {**INVOICE_FILES, ".gitignore": "__pycache__/\n"})
    base = sh(repo, "git", "rev-parse", "HEAD").strip()
    totals = repo / "invoice" / "totals.py"
    totals.write_text(totals.read_text().replace(BUG, FIX).replace(
        "    # BUG: truncates instead of rounding half up\n", ""))
    (repo / "tests" / "test_discount.py").write_text(HIDDEN)
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-q", "-m", "Round discounts half up to the cent (fixes #7)")
    ref = sh(repo, "git", "rev-parse", "HEAD").strip()
    return repo, base, ref


@pytest.fixture
def archive(tmp_path, product):
    """An eval archive with one set ('demo') holding the example case, and two scripted variants."""
    repo, base, ref = product
    root = tmp_path / "archive" / "evals"
    cases = root / "sets" / "demo" / "cases"
    cases.mkdir(parents=True)
    text = EXAMPLE.read_text().replace("{repo}", str(repo)).replace("{base}", base).replace("{reference}", ref)
    data = yaml.safe_load(text)
    data["config"] = yaml.safe_load(invoice_config())
    (cases / "invoice-discount-rounding.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    (root / "variants").mkdir()
    (root / "variants" / "good.yaml").write_text(yaml.safe_dump(scripted_variant(implementer(FIX))))
    (root / "variants" / "gaming.yaml").write_text(yaml.safe_dump(scripted_variant(implementer(GAMING))))
    return root


def fake_claude(tmp_path: Path, new_code: str, cost: float = 0.42, result: str = "Done.",
                is_error: bool = False) -> Path:
    """A stand-in for `claude -p`: edits the checkout it runs in and prints Claude Code's JSON."""
    script = tmp_path / "fake-claude"
    out = json.dumps({"type": "result", "is_error": is_error, "result": result, "num_turns": 4,
                      "total_cost_usd": cost, "permission_denials": [{"tool_name": "WebFetch"}]})
    edit = textwrap.dedent(f"""
        import pathlib, sys
        prompt = sys.stdin.read()
        p = pathlib.Path("invoice/totals.py")
        p.write_text(p.read_text().replace({BUG!r}, {new_code!r}))
        if {new_code != BUG!r}:
            pathlib.Path("tests/test_rounding.py").write_text(
                "from invoice.totals import apply_discount\\n\\ndef test_it():\\n    assert apply_discount(999, 50) == 500\\n")
        pathlib.Path({str(tmp_path / 'prompt-received.txt')!r}).write_text(prompt)
    """)
    script.write_text(f"#!/bin/sh\n{PY} -c {shlex.quote(edit)}\necho {shlex.quote(out)}\n" if not is_error else
                      f"#!/bin/sh\ncat >/dev/null\necho {shlex.quote(out)}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


# ----------------------------------------------------------------------------- cases and archive

def test_example_case_parses_and_ticket_text_is_the_requesters(archive):
    case = EvalSet(archive, "demo").cases()[0]
    assert case.id == "invoice-discount-rounding" and case.ticket.kind == "bug"
    assert case.ticket.text().startswith("Discount truncates instead of rounding\n\napply_discount(999, 50)")
    assert "Acceptance criteria" not in case.ticket.text()
    with pytest.raises(EvalError, match="missing"):
        from asf.evals.cases import parse_case
        parse_case({"repo": "x", "ticket": {"title": "t"}})


def test_archive_refused_inside_a_product_repo_or_this_repo(product, tmp_path):
    repo, _, _ = product
    with pytest.raises(EvalError, match="inside the repository"):
        guard_archive(repo / "evals", [repo])
    with pytest.raises(EvalError, match="inside the repository"):
        guard_archive(Path(__file__).resolve().parents[1] / "evals")
    guard_archive(tmp_path / "elsewhere", [repo])  # fine


def test_checkout_has_no_reference_history_and_no_remote(home, archive, product, tmp_path):
    repo, base, ref = product
    case = EvalSet(archive, "demo").cases()[0]
    co, got = workspace.checkout(case, tmp_path / "co")
    assert got == base
    assert sh(co, "git", "rev-parse", "HEAD").strip() == base
    assert sh(co, "git", "remote").strip() == ""
    # The reference commit (and so the hidden tests) cannot be found from the checkout.
    assert sh(co, "git", "log", "--all", "--format=%H").split() == [base]
    assert not workspace.has_commit(co, ref)
    assert not (co / "tests" / "test_discount.py").exists()
    # The product repository was only read.
    assert sh(repo, "git", "status", "--porcelain") == ""


def test_hidden_tests_fail_on_base_and_pass_on_reference(home, archive, capsys):
    rc = cli.main(["eval", "--evals", str(archive), "check", "--set", "demo"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "ok    invoice-discount-rounding: 1 hidden test file(s); 2 fail-to-pass, 1 pass-to-pass" in out
    check = json.loads((archive / "sets" / "demo" / "checks" / "invoice-discount-rounding.json").read_text())
    assert check["hidden_tests"] == ["tests/test_discount.py"] and not check["problems"]
    assert check["fail_to_pass"] == ["tests/test_discount.py::test_half_cent_rounds_up",
                                     "tests/test_discount.py::test_one_cent_half_off"]
    assert check["pass_to_pass"] == ["tests/test_discount.py::test_whole"] and check["flaky"] == []


def test_parse_counts():
    assert score.parse_counts("..F\n==== 1 failed, 2 passed in 0.1s ====", False) == (2, 3)
    assert score.parse_counts("==== 3 passed in 0.1s ====", True) == (3, 3)
    assert score.parse_counts("ERROR collecting\n==== 1 error in 0.1s ====", False) == (0, 1)
    assert score.parse_counts("Tests:       1 failed, 4 passed, 5 total", False) == (4, 5)
    assert score.parse_counts("--- PASS: TestA\n--- FAIL: TestB\n", False) == (1, 2)
    assert score.parse_counts("boom", False) == (0, 1)


# ----------------------------------------------------------------------------- variants

def test_variants_overlay_and_availability(archive):
    vs = variants.available(archive)
    assert {"current", "claude_code", "claude_code_native", "cursor_native", "opus-high", "interactive",
            "good", "gaming"} <= set(vs)
    base = {"models": {"provider": "pool", "backends": {"cursor": {"provider": "cursor_cli"}}},
            "budgets": {"tier_2": {"usd": 9}}}
    applied = vs["opus-high"].apply(base)
    assert applied["models"]["provider"] == "claude_code" and "backends" not in applied["models"]
    assert applied["budgets"] == {"tier_2": {"usd": 9}}
    assert vs["opus-high"].backends(base) == ["claude"]
    assert vs["current"].backends(base) == ["cursor"]
    assert vs["current"].unavailable(base) is None
    # A provider this build lacks (the native ones, until their branch merges) is reported, not crashed on.
    missing = variants.Variant("x", overlay={"models": {"provider": "no_such_provider"}}, replace=["models"])
    assert "no_such_provider" in missing.unavailable({})
    with pytest.raises(EvalError, match="unknown variant"):
        variants.load(archive, "nope")


def test_eval_config_forces_shadow_mode_and_local_delivery():
    raw = yaml.safe_load(invoice_config(**{"autonomy.shadow": False, "autonomy.max_merge_tier": 3,
                                           "delivery.provider": "github",
                                           "intake.github_issues": {"enabled": True}}))
    cfg = workspace.eval_config(raw)
    assert cfg.get("autonomy.shadow") is True and cfg.get("autonomy.max_merge_tier") == -1
    assert cfg.get("delivery.provider") == "local_git"
    assert list(cfg.get("intake")) == ["local"]
    assert cfg.default_branch == "main" and cfg.get("deploy.steps") == []


# ----------------------------------------------------------------------------- running

def test_run_and_report_end_to_end(home, archive, product, capsys):
    """Two scripted factory variants through the real pipeline, in parallel, then the report."""
    repo, base, _ = product
    rc = cli.main(["eval", "--evals", str(archive), "run", "--set", "demo", "--config", "good,gaming",
                   "--jobs", "2", "--run-id", "r1"])
    out = capsys.readouterr().out
    assert rc == 0, out
    res = {v: json.loads((archive / "sets/demo/runs/r1" / v / "invoice-discount-rounding-r1/result.json").read_text())
           for v in ("good", "gaming")}
    good, gaming = res["good"], res["gaming"]
    for r in (good, gaming):
        assert r["final_state"] == "awaiting_review" and r["delivered"] and r["visible_pass"]
        assert r["gates_first_try"] is True and r["fix_rounds"] == 0 and r["cost_usd"] > 0
        assert r["diff"]["code_files"] == ["invoice/totals.py"] and r["diff"]["ref_files_touched"] == 1.0
    assert good["hidden"]["all_pass"] and good["hidden"]["scored"] == "per test"
    assert (good["hidden"]["fixed"], good["hidden"]["fail_to_pass"], good["hidden"]["broken"]) == (2, 2, [])
    # The gaming fix passes its own test and every gate, but not the hidden tests: the gap signal.
    assert not gaming["hidden"]["all_pass"] and gaming["hidden"]["fixed"] == 1
    assert gaming["hidden"]["not_fixed"] == ["tests/test_discount.py::test_one_cent_half_off"]
    run_dir = archive / "sets/demo/runs/r1/good/invoice-discount-rounding-r1"
    assert "Round discounts" in (run_dir / "report.md").read_text()
    assert (run_dir / "candidate.diff").read_text().count("apply_discount") >= 1
    assert any(json.loads(line)["type"] == "gate.result" for line in (run_dir / "events.jsonl").open())
    assert json.loads((archive / "sets/demo/runs/r1/run.json").read_text())["repeat"] == 1
    # Never merged, never pushed: the product repo is untouched and work folders are cleaned up.
    assert sh(repo, "git", "rev-parse", "main").strip() == sh(repo, "git", "rev-parse", "HEAD").strip()
    assert sh(repo, "git", "branch", "--list", "factory/*").strip() == ""
    assert not list((home / "evals" / "work").rglob("factory.db"))

    rc = cli.main(["eval", "--evals", str(archive), "report", "--set", "demo", "--baseline", "good"])
    text = capsys.readouterr().out
    assert rc == 0
    assert "visible-hidden gap" in text and "gaming" in text
    latest = json.loads((archive / "sets/demo/reports/latest.json").read_text())
    assert latest["variants"]["gaming"]["metrics"]["gap"]["mean"] == 1.0
    assert latest["variants"]["good"]["metrics"]["gap"]["mean"] == 0.0
    assert latest["comparisons"]["gaming"]["resolved"]["verdict"] == "too few cases (1)"
    assert any("Only 1 case" in f for f in latest["flags"])
    assert (archive / "sets/demo/reports/latest.md").read_text().startswith("# Eval report: demo")


def test_interactive_baseline_runs_claude_on_the_ticket_text_alone(home, archive, tmp_path):
    binary = fake_claude(tmp_path, FIX)
    v = variants.Variant("interactive", kind="interactive", settings={"binary": str(binary)})
    es = EvalSet(archive, "demo")
    [r] = runner.run_set(es, [v], es.cases(), runner.Options(run_id="b1", echo=lambda m: None))
    assert r["final_state"] == "finished" and r["delivered"]
    assert r["hidden"]["all_pass"] and r["visible_pass"] and r["cost_usd"] == 0.42 and r["denials"] == 1
    assert {g["name"] for g in r["gates"]} >= {"test", "test_integrity"}
    prompt = (tmp_path / "prompt-received.txt").read_text()
    assert prompt == es.cases()[0].ticket.text()  # the ticket and nothing else


def test_interactive_baseline_that_changes_nothing(home, archive, tmp_path):
    binary = fake_claude(tmp_path, BUG)
    v = variants.Variant("interactive", kind="interactive", settings={"binary": str(binary)})
    es = EvalSet(archive, "demo")
    [r] = runner.run_set(es, [v], es.cases(), runner.Options(run_id="b2", echo=lambda m: None))
    assert r["final_state"] == "no_changes" and not r["delivered"] and not r["visible_pass"]
    assert r["hidden"]["fixed"] == 0 and not r["hidden"]["all_pass"]


def test_usage_limit_skips_without_waiting_and_budget_cap(home, archive, tmp_path):
    from datetime import datetime, timedelta
    limits.record_limit("claude", datetime.now().astimezone() + timedelta(hours=1), "hit your limit")
    v = variants.Variant("interactive", kind="interactive", settings={"binary": str(fake_claude(tmp_path, FIX))})
    es = EvalSet(archive, "demo")
    [r] = runner.run_set(es, [v], es.cases(), runner.Options(run_id="l1", wait=False, echo=lambda m: None))
    assert "usage limit on claude" in r["skipped"]
    waits = []
    with pytest.raises(RuntimeError):
        runner._wait_for_backends(["claude"], runner.Options(sleep=lambda s: (waits.append(s), (_ for _ in ()).throw(
            RuntimeError("slept")))), lambda m: None)
    assert waits and waits[0] <= 600
    # A run with its dollar cap spent starts nothing more.
    opts = runner.Options(run_id="c1", max_usd=0.0, echo=lambda m: None)
    [r] = runner.run_set(es, [variants.load(archive, "good")], es.cases(), opts)
    assert "eval budget" in r["skipped"]


def test_unavailable_variant_is_skipped(home, archive):
    es = EvalSet(archive, "demo")
    v = variants.Variant("native", overlay={"models": {"provider": "no_such_provider"}}, replace=["models"])
    with pytest.raises(EvalError, match="no variant can run"):
        runner.run_set(es, [v], es.cases(), runner.Options(echo=lambda m: None))


# ----------------------------------------------------------------------------- mining

def test_mine_keeps_small_changes_with_code_and_tests(tmp_path, capsys):
    from conftest import INVOICE_FILES
    repo = make_repo(tmp_path / "invoice", INVOICE_FILES)

    def commit(files: dict, msg: str) -> str:
        for rel, content in files.items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(content)
        sh(repo, "git", "add", "-A")
        sh(repo, "git", "commit", "-q", "-m", msg)
        return sh(repo, "git", "rev-parse", "HEAD").strip()

    good = commit({"invoice/totals.py": "def f():\n    return 2\n", "tests/test_f.py": "def test_f():\n    pass\n"},
                  "Return two from f for the new pricing rule\n\nDetails here.\n\nCo-Authored-By: x <x@y>")
    commit({"README.md": "# docs only\n"}, "Document the invoice module in more detail")
    commit({"invoice/auth.py": "def check(t):\n    return False\n"}, "Lock everything down for the audit")
    commit({"invoice/big.py": "x = 1\n" * 500, "tests/test_big.py": "def test_big():\n    pass\n"},
           "Add a very large generated table")
    commit({"invoice/totals.py": "def f():\n    return 3\n", "tests/test_f.py": "def test_f():\n    pass  # 3\n"},
           "fix")
    # A merged pull request is one unit: base is its first parent.
    sh(repo, "git", "checkout", "-q", "-b", "feature")
    commit({"invoice/csv.py": "def export():\n    return ''\n", "tests/test_csv.py": "def test_csv():\n    pass\n"},
           "wip")
    sh(repo, "git", "checkout", "-q", "main")
    before = sh(repo, "git", "rev-parse", "HEAD").strip()
    sh(repo, "git", "merge", "-q", "--no-ff", "feature", "-m", "Merge pull request #12 from me/feature\n\nAdd CSV export for invoices")
    merge = sh(repo, "git", "rev-parse", "HEAD").strip()

    drafts = mine_mod.mine(repo, limit=10)
    assert [(d.reference, d.base) for d in drafts] == [(merge, before), (good, sh(repo, "git", "rev-parse", f"{good}^").strip())]
    pr, plain = drafts
    assert pr.ticket.title == "Add CSV export for invoices" and pr.ticket.kind == "feature"
    assert plain.ticket.body == "Details here." and plain.extra["review"] == "needed"
    assert plain.extra["mined"]["test_files"] == ["tests/test_f.py"]

    archive = tmp_path / "archive"
    rc = cli.main(["eval", "--evals", str(archive), "mine", "--repo", str(repo), "--limit", "5"])
    assert rc == 0
    written = sorted((archive / "sets" / "invoice" / "drafts").glob("*.yaml"))
    assert len(written) == 2 and load_case(written[0]).reference in (merge, good)
    assert "2 draft(s)" in capsys.readouterr().out
    # Drafting into the product repository itself is refused.
    assert cli.main(["eval", "--evals", str(repo / "evals"), "mine", "--repo", str(repo)]) == 2


# ----------------------------------------------------------------------------- report

def _result(variant, case, k, *, delivered=True, visible=True, hidden=(3, 3), cost=1.0, minutes=5.0, **kw):
    passed, total = hidden
    return {"variant": variant, "case": case, "repeat": k, "kind": "factory", "final_state": "awaiting_review",
            "delivered": delivered, "visible_pass": visible, "gates_first_try": visible, "fix_rounds": 1,
            "denials": 0, "cost_usd": cost, "minutes": minutes,
            "hidden": {"passed": passed, "total": total, "rate": passed / total, "all_pass": passed == total},
            "diff": {"size_ratio": 1.0}, **kw}


def test_report_pairs_by_case_and_flags_noise():
    results = []
    for i in range(6):
        for k in (1, 2):
            results.append(_result("base", f"c{i}", k, hidden=(1, 3), visible=True))
            results.append(_result("better", f"c{i}", k, hidden=(3, 3), cost=2.0))
            # Same as base on average, but noisy between cases.
            results.append(_result("noisy", f"c{i}", k, hidden=((3, 3) if i % 2 else (0, 3))))
    results.append({"variant": "better", "case": "c0", "repeat": 3, "skipped": "usage limit"})
    rep = report_mod.summarize(results, baseline="base")
    b = rep["variants"]["base"]
    assert b["cases"] == 6 and b["runs"] == 12 and b["repeats"] == 2
    assert b["metrics"]["gap"]["mean"] == 1.0  # visible pass every time, hidden never all pass
    assert rep["variants"]["better"]["metrics"]["gap"]["mean"] == 0.0
    assert rep["variants"]["better"]["skipped"] == 1
    cmp = rep["comparisons"]
    assert cmp["better"]["resolved"]["verdict"] == "consistent" and cmp["better"]["resolved"]["diff"] == 1.0
    assert cmp["better"]["cost_usd"]["diff"] == 1.0
    assert cmp["noisy"]["hidden_rate"]["verdict"] == "within noise"
    assert any("Only 6 case" in f for f in rep["flags"])
    assert any("1 skipped" in f for f in rep["flags"])
    md = report_mod.render(rep, "demo", md=True)
    assert "| better |" in md and "Against base (paired by case)" in md
    assert "within noise" in report_mod.render(rep, "demo")


def test_mine_strips_closing_git_trailers_only():
    body = "Details here.\n\nNote: this paragraph stays.\n\nCo-Authored-By: x <x@y>\nReplit-Commit-Author: Agent"
    assert mine_mod.strip_trailers(body) == "Details here.\n\nNote: this paragraph stays."


def test_test_ids_and_classify():
    py = "PASSED tests/t.py::test_a\nFAILED tests/t.py::test_b - assert 1 == 2\nERROR tests/u.py - ImportError\n"
    assert score.test_ids(py, "tests/t.py") == {"tests/t.py::test_a": True, "tests/t.py::test_b": False,
                                                "tests/u.py": False}
    go = "=== RUN TestA\n--- PASS: TestA (0.00s)\n    --- FAIL: TestA/sub (0.00s)\n"
    assert score.test_ids(go, "pkg/a_test.go") == {"pkg/a_test.go::TestA": True, "pkg/a_test.go::TestA/sub": False}
    js = " ✓ src/a.test.ts > adds 2ms\n × src/a.test.ts > subtracts 3ms\n"
    assert score.test_ids(js, "src/a.test.ts") == {"src/a.test.ts::src/a.test.ts > adds": True,
                                                   "src/a.test.ts::src/a.test.ts > subtracts": False}
    assert score.verbose("go test -race ./x") == "go test -v -race ./x"
    assert score.verbose("python -m pytest -q x.py") == "python -m pytest -q x.py -rA"
    got = score.classify({"a": False, "b": True, "c": True},
                         [{"a": True, "b": True, "c": True, "d": False, "e": True},
                          {"a": True, "b": True, "c": False, "d": False, "e": True}])
    assert got == {"fail_to_pass": ["a", "e"], "pass_to_pass": ["b"], "fails_on_reference": ["d"], "flaky": ["c"]}


def test_authored_case_with_reference_patch_and_hidden_dir(home, archive, product, tmp_path, capsys):
    """A case written for the eval: its own reference (a patch) and its own hidden tests, aimed at an
    edge the original tests missed. The product repository never sees either."""
    repo, base, _ = product
    folder = archive / "sets" / "demo" / "cases" / "authored"
    (folder / "hidden" / "tests").mkdir(parents=True)
    (folder / "hidden" / "tests" / "test_edges.py").write_text(
        "from invoice.totals import apply_discount\n\n\ndef test_negative_percent_rejected():\n"
        "    import pytest\n    with pytest.raises(ValueError):\n        apply_discount(100, -5)\n\n\n"
        "def test_plain():\n    assert apply_discount(1000, 10) == 900\n")
    (folder / "ref.diff").write_text(
        "--- a/invoice/totals.py\n+++ b/invoice/totals.py\n@@ -8,2 +8,4 @@ def apply_discount(cents, percent):\n"
        "     # BUG: truncates instead of rounding half up\n+    if percent < 0:\n+        raise ValueError(percent)\n"
        "     return int(cents * (100 - percent) / 100)\n")
    case = {"id": "authored-negative", "repo": str(repo), "base": base, "reference_patch": "authored/ref.diff",
            "hidden_dir": "authored/hidden", "tags": ["authored", "edge-case"], "reference_by": "human",
            "ticket": {"title": "Negative discounts add money", "body": "apply_discount(100, -5) returns 105.",
                       "kind": "bug"},
            "config": yaml.safe_load(invoice_config())}
    (archive / "sets" / "demo" / "cases" / "authored-negative.yaml").write_text(yaml.safe_dump(case))
    assert cli.main(["eval", "--evals", str(archive), "check", "--set", "demo", "--cases", "authored-negative"]) == 0
    assert "1 fail-to-pass, 1 pass-to-pass" in capsys.readouterr().out
    assert sh(repo, "git", "for-each-ref", "refs/asf-eval").strip() == ""  # made in the eval mirror only
    binary = fake_claude(tmp_path, FIX)
    v = variants.Variant("interactive", kind="interactive", settings={"binary": str(binary)})
    es = EvalSet(archive, "demo")
    [r] = runner.run_set(es, [v], es.cases(["authored-negative"]), runner.Options(run_id="a1", echo=lambda m: None))
    # The rounding fix does nothing for negative percents: the authored edge test catches it.
    assert r["hidden"]["scored"] == "per test" and r["hidden"]["fixed"] == 0 and r["tags"] == ["authored", "edge-case"]
    rep = report_mod.summarize(report_mod.load_results(es))
    assert rep["by_tag"]["edge-case"]["interactive"]["mean"] == 0.0 and "by:human" in rep["by_tag"]


def test_cursor_variants_run_grok():
    vs = variants.available(Path("/nonexistent"))
    for name in ("cursor_cli", "cursor_native"):
        roles = vs[name].apply({})["models"]["roles"]
        assert {r["model"] for r in roles.values()} <= {"grok-4.7-low", "grok-4.7-medium", "grok-4.7-high"}
        assert roles["implementer"]["model"] == "grok-4.7-high"
        assert vs[name].backends({}) == ["cursor"]


def test_interface_mismatch_is_detected_and_left_out_of_quality_numbers():
    log = "./tok_test.go:12:5: undefined: encodeToken\nE   ImportError: cannot import name '_real_question'"
    assert score.interface_mismatch(log) == ["encodeToken", "_real_question"]
    ok = _result("v", "c1", 1)
    mismatched = _result("v", "c2", 1, hidden=(0, 3))
    mismatched["hidden"]["interface_mismatch"] = ["encodeToken"]
    rep = report_mod.summarize([ok, mismatched])
    s = rep["variants"]["v"]
    assert s["interface_mismatch"] == 1 and s["metrics"]["resolved"]["n"] == 1
    assert s["metrics"]["resolved"]["mean"] == 1.0 and s["metrics"]["cost_usd"]["n"] == 2
    assert any("different interface" in f for f in rep["flags"])
