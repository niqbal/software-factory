"""Guided setup: machine checks, project drafts with options, the CLI and the console's Set up page."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
import yaml

from asf import cli, setup_wizard as sw
from asf.ui import Portfolio, make_handler
from conftest import INVOICE_FILES, make_repo, sh


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    d = tmp_path / "asf-home"
    monkeypatch.setenv("ASF_HOME", str(d))
    # No real CLI is consulted: tests decide what is installed and signed in.
    monkeypatch.setattr(sw, "_cli", lambda name: None)
    return d


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "work" / "invoice", {
        **INVOICE_FILES, "pyproject.toml": "[project]\nname='invoice'\n", "requirements-dev.txt": "pytest\n"})


def by_id(checks):
    return {c.id: c for c in checks}


def test_machine_needs_one_backend(monkeypatch):
    checks = by_id(sw.machine_checks(env={"PATH": "/usr/bin:/bin"}))
    assert checks["python"].status == "ok" and checks["git"].status == "ok"
    assert checks["backend"].status == "fail" and checks["backend"].required
    assert "claude auth login" in checks["backend"].fix
    assert not sw.ready(list(checks.values()))

    assert sw.ready(sw.machine_checks(env={"PATH": "/usr/bin:/bin", "ANTHROPIC_API_KEY": "sk-test"}))

    monkeypatch.setattr(sw, "_cli", lambda name: "/bin/claude" if name == "claude" else None)
    monkeypatch.setattr(sw, "claude_status", lambda b: (True, "signed in (claude.ai)"))
    checks = by_id(sw.machine_checks(env={"PATH": "/usr/bin:/bin"}))
    assert checks["claude"].status == "ok" and checks["backend"].detail == "Claude Code"
    # Optional tools never make the machine "not ready".
    assert checks["cursor"].status == "info" and not checks["cursor"].required


def test_claude_status_reads_auth_json(monkeypatch):
    class R:
        def __init__(self, out):
            self.stdout, self.stderr, self.ok = out, "", True
    monkeypatch.setattr(sw, "run", lambda *a, **k: R('{"loggedIn": true, "authMethod": "claude.ai", "email": "x@y"}'))
    ok, how = sw.claude_status("claude")
    assert ok and how == "signed in (claude.ai)" and "x@y" not in how
    monkeypatch.setattr(sw, "run", lambda *a, **k: R('{"loggedIn": false}'))
    assert sw.claude_status("claude") == (False, "not signed in")


def test_draft_applies_options_and_writes_nothing(repo, home):
    d = sw.draft(repo, {"backend": "anthropic", "github_issues": True, "label": "agents", "github_prs": True,
                        "name": "Invoices"})
    cfg = yaml.safe_load(d["yaml"])
    assert cfg["models"] == {"provider": "anthropic"} and "budgets" not in cfg
    assert cfg["intake"]["github_issues"] == {"enabled": True, "label": "agents", "write_back": False}
    assert cfg["delivery"]["provider"] == "github" and cfg["project"]["name"] == "Invoices"
    assert any("not a GitHub remote" in w for w in d["warnings"])
    assert d["summary"]["packages"][0]["commands"]["test"]
    assert "CLAUDE.md" in d["summary"]["instruction_files"]
    assert not home.exists() or not any(home.iterdir())

    pooled = yaml.safe_load(sw.draft(repo, {"backend": "pool"})["yaml"])
    assert pooled["models"]["provider"] == "pool" and pooled["budgets"]["tier_2"]["minutes"]


def test_draft_refuses_what_cannot_be_a_project(tmp_path):
    with pytest.raises(ValueError, match="not a git repository"):
        sw.draft(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    sh(empty, "git", "init", "-q")
    with pytest.raises(ValueError, match="no commits"):
        sw.draft(empty)
    with pytest.raises(ValueError, match="backend must be"):
        sw.draft(make_repo(tmp_path / "r", {"a.py": ""}), {"backend": "gpt"})


def test_create_registers_commits_branch_and_checks(repo, home):
    result = sw.create(repo, {"backend": "anthropic", "name": "Invoices", "priority": 3})
    sd = home / result["key"]
    assert (sd / "factory.yaml").exists() and (sd / "factory.db").exists()
    assert json.loads((sd / "project.json").read_text())["repo"] == str(repo.resolve())
    assert yaml.safe_load((home / "projects.yaml").read_text())["projects"][result["key"]] == \
        {"name": "Invoices", "priority": 3}
    assert result["branch"] == "factory/setup" and result["commit"]
    assert "factory.yaml" in sh(repo, "git", "show", "--name-only", "--format=", "factory/setup")
    assert sh(repo, "git", "status", "--porcelain") == ""  # the checkout is untouched
    checks = {c["id"]: c for c in result["checks"]}
    assert checks["config"]["status"] == "ok" and "pending draft" in checks["config"]["detail"]
    assert checks["models"]["status"] == "fail"  # no API key in this test's environment
    assert not result["healthy"]

    again = sw.create(repo, {"backend": "claude"})
    assert any("kept the existing draft" in n for n in again["notes"]) and not again["commit"]
    assert "anthropic" in (sd / "factory.yaml").read_text()
    replaced = sw.create(repo, {"backend": "claude", "force": True})
    assert "claude_code" in (sd / "factory.yaml").read_text() and replaced["commit"]


def test_candidate_repos_marks_registered(repo, tmp_path):
    sw.create(repo, {"backend": "anthropic", "branch": False})
    found = sw.candidate_repos(roots=[str(tmp_path / "work")])
    assert [r["name"] for r in found] == ["invoice"] and found[0]["project"]


def test_cli_setup_add_and_check(repo, home, capsys, monkeypatch):
    assert cli.main(["setup", "add", str(repo), "--backend", "anthropic", "--print"]) == 0
    assert "provider: anthropic" in capsys.readouterr().out
    assert not home.exists() or not any(home.iterdir())

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    rc = cli.main(["setup", "add", str(repo), "--backend", "anthropic", "--no-branch", "--name", "Inv"])
    out = capsys.readouterr().out
    assert "registered as" in out and "Next:" in out and rc == 0
    assert cli.main(["--repo", str(repo), "doctor"]) == 0
    assert "healthy" in capsys.readouterr().out

    assert cli.main(["setup", "check", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ready"] is True


def start(portfolio):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(portfolio))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def call(url, body=None, token=None):
    if body is None:
        return json.loads(urllib.request.urlopen(url).read())
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **({"X-ASF-Token": token} if token else {})})
    return json.loads(urllib.request.urlopen(req).read())


def test_console_setup_flow(repo, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    portfolio = Portfolio()
    server, base = start(portfolio)
    t = portfolio.token
    try:
        page = urllib.request.urlopen(base + "/").read().decode()
        assert 'id="setupView"' in page and "#setup" in page
        m = call(base + "/api/setup/machine")
        assert m["ready"] and {c["id"] for c in m["checks"]} >= {"python", "git", "backend"}

        with pytest.raises(urllib.error.HTTPError) as e:
            call(base + "/api/setup/create", {"repo": str(repo)})  # no token
        assert e.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as e:
            call(base + "/api/setup/draft", {"repo": str(repo.parent)}, t)
        assert e.value.code == 400

        d = call(base + "/api/setup/draft", {"repo": str(repo), "options": {"backend": "anthropic"}}, t)
        assert d["summary"]["backend"] == "anthropic" and "factory.yaml" not in str(list(repo.iterdir()))
        r = call(base + "/api/setup/create", {"repo": str(repo), "options": {"backend": "anthropic", "branch": False}}, t)
        assert r["healthy"] and r["message"].startswith("added")
        assert r["key"] in [p["key"] for p in call(base + "/api/portfolio")["projects"]]
        assert call(base + "/api/setup/check", {"key": r["key"]}, t)["healthy"]

        q = call(f"{base}/api/p/{r['key']}/items",
                 {"title": "Round discounts half up", "acceptance": "a\n\nb", "kind": "bug"}, t)
        job = portfolio.console(r["key"]).f.store.job(q["job"])
        assert job["state"] == "queued"
        item = portfolio.console(r["key"]).f.store.get_work_item(job["work_item_id"])
        assert item.acceptance == ["a", "b"] and item.kind == "bug"
        with pytest.raises(urllib.error.HTTPError) as e:
            call(f"{base}/api/p/{r['key']}/items", {"title": " "}, t)
        assert e.value.code == 400
    finally:
        server.shutdown()


def test_terminal_wizard(repo, home, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(sw, "candidate_repos", lambda: [{"path": str(repo), "name": "invoice", "project": None}])
    answers = iter(["1", "Inv", "2", "y", "anthropic", "y", "agents", "n", "n", "n",  # repo, name, priority, options
                    "y",                                                           # add it
                    "Round discounts half up", "rounds 999 at 15% to 849; keeps cents",  # first item
                    "n"])                                                          # no console
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    assert cli.main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "1. This machine" in out and "3. Project checks" in out and "queued JOB-" in out
    key = sw.state_dir(repo).name
    cfg = yaml.safe_load((home / key / "factory.yaml").read_text())
    assert cfg["intake"]["github_issues"]["label"] == "agents" and cfg["delivery"]["provider"] == "local_git"
    assert yaml.safe_load((home / "projects.yaml").read_text())["projects"][key]["priority"] == 2
    assert "factory/setup" not in sh(repo, "git", "branch")  # the wizard was told not to commit a branch
