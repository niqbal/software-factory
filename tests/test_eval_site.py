import json
import re

from asf.evals import site


def _result(case, variant, k, delivered=True, all_pass=True, **extra):
    return {"case": case, "variant": variant, "repeat": k, "run_id": "r1", "delivered": delivered,
            "visible_pass": True, "cost_usd": 1.5, "minutes": 3.0, "final_state": "awaiting_review",
            "hidden": {"rate": 1.0 if all_pass else 0.0, "all_pass": all_pass, "fixed": 1 if all_pass else 0,
                       "fail_to_pass": 1, "broken": []}, "finished": 100.0, **extra}


def test_site_has_every_sample_the_plan_and_the_runs_in_progress(tmp_path):
    root = tmp_path / "evals"
    sdir = root / "sets" / "v1"
    (sdir / "cases").mkdir(parents=True)
    (sdir / "cases" / "c1.yaml").write_text("id: c1\nticket:\n  title: Fix it\ntags: [mined]\n")
    run = sdir / "runs" / "r1"
    for variant, k, kw in [("claude_code", 1, {}), ("claude_code", 2, {"all_pass": False}),
                           ("cursor_cli", 1, {"delivered": False, "all_pass": False}),
                           ("cursor_cli", 2, {"error": "ETIMEDOUT"})]:
        d = run / variant / f"c1-r{k}"
        d.mkdir(parents=True)
        (d / "result.json").write_text(json.dumps(_result("c1", variant, k, **kw)))
    going = run / "claude_code" / "c1-r3"
    going.mkdir()
    (going / "events.jsonl").write_text(json.dumps({"type": "implementing"}) + "\n")
    (run / "PAUSE.claude").write_text("")
    (run / "run.json").write_text(json.dumps({"started": 1, "asf": "abc", "variants": {"claude_code": {"hash": "h1"}},
                                              "plan": {"claude_code": {"cases": ["c1"], "repeat": 3}}}))
    (sdir / "reports").mkdir()
    (sdir / "reports" / "2026.md").write_text("# Report </script>")

    page = site.build(root, tmp_path / "out").read_text()
    data = json.loads(re.search(r"const DATA = (\{.*?\});\n", page).group(1).replace("<\\/", "</"))

    outcomes = {(s["variant"], s["repeat"]): s["outcome"] for s in data["samples"]}
    assert outcomes == {("claude_code", 1): "resolved", ("claude_code", 2): "partial",
                        ("cursor_cli", 1): "not_delivered", ("cursor_cli", 2): "error"}
    (r,) = data["runs"]
    assert r["plan"]["claude_code"]["repeat"] == 3 and r["paused"] == ["PAUSE.claude"]
    assert [(a["variant"], a["repeat"], a["step"]) for a in r["active"]] == [("claude_code", 3, "implementing")]
    assert data["sets"][0]["cases"]["c1"]["title"] == "Fix it"
    assert "</script>" not in page.split("const DATA = ")[1].split(";\n")[0]  # report text cannot end the script
