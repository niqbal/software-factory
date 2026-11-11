import json

from asf import config as config_mod
from asf import setup_agent
from conftest import make_repo


def test_node_monorepo_with_composite_tests(tmp_path):
    pkg = {"scripts": {"test": "npm run test:server && npm run test:client",
                       "test:server": "vitest run --config vitest.server.config.ts",
                       "test:client": "vitest run --config vitest.client.config.ts",
                       "check": "tsc", "build": "vite build"},
           "devDependencies": {"typescript": "5", "vitest": "1"}}
    repo = make_repo(tmp_path / "mono", {
        "package.json": json.dumps(pkg), "package-lock.json": "{}",
        "tsconfig.json": json.dumps({"include": ["client/src/**/*", "server/**/*", "shared/**/*"]}),
        "server/auth.ts": "x", "server/index.ts": "x", "client/src/App.tsx": "x", "shared/schema.ts": "x",
        "mobile/package.json": json.dumps({"scripts": {"test": "jest"}}), "mobile/src/a.ts": "x",
        "migrations/001.sql": "x"})
    cfg = config_mod.load(text=setup_agent.render(setup_agent.detect(repo)))
    by = {p.name: p for p in cfg.packages}
    assert set(by) == {"app", "server", "client", "mobile"}
    assert by["server"].commands["test"] == "npx vitest run --config vitest.server.config.ts {args}"
    assert by["app"].commands["typecheck"] == "npx tsc"
    assert by["mobile"].root == "mobile" and by["mobile"].commands["test"] == "npx jest {args}"
    assert not by["server"].owns("mobile/src/a.ts")
    assert [p.name for p in cfg.packages if p.owns("client/src/App.tsx")] == ["app", "client"]
    rules = {r["tier"]: r["paths"] for r in cfg.get("tiers.rules")}
    assert "server/auth.ts" in rules[3] and "migrations/**" in rules[3]


def test_python_and_go(tmp_path):
    py = make_repo(tmp_path / "py", {"pyproject.toml": "[project]\nname='x'\n[tool.ruff]\n",
                                     "requirements-dev.txt": "pytest\nruff\n", "x/__init__.py": "",
                                     "tests/test_x.py": "def test(): pass\n"})
    cfg = setup_agent.detect(py)
    cmds = cfg["packages"][0]["commands"]
    assert "pytest" in cmds["test"] and "ruff" in cmds["lint"]
    go = make_repo(tmp_path / "go", {"go.mod": "module x\n", "main.go": "package main\n"})
    assert setup_agent.detect(go)["packages"][0]["commands"]["test"].startswith("go test")


def test_models_drafted_from_installed_clis():
    both = setup_agent.draft_models(claude="/bin/claude", cursor="/bin/cursor-agent")
    assert both["provider"] == "pool" and list(both["backends"]) == ["cursor", "claude"]
    assert both["routing"]["reviewer"] == ["claude", "cursor"]
    assert setup_agent.draft_models(claude="/bin/claude", cursor=None)["provider"] == "claude_code"
    assert setup_agent.draft_models(claude=None, cursor="/bin/cursor-agent")["provider"] == "cursor_cli"
    assert setup_agent.draft_models(claude=None, cursor=None) == {"provider": "anthropic"}
    for m in (both, {"provider": "anthropic"}):
        config_mod.load(text=json.dumps({"version": 1, "project": {"name": "x"}, "models": m}))


def test_init_registers_the_project(tmp_path, monkeypatch):
    from asf import cli, projects
    monkeypatch.setenv("ASF_HOME", str(tmp_path / "home"))
    repo = make_repo(tmp_path / "game", {"package.json": json.dumps({"scripts": {"test": "node --test"}})})
    assert cli.main(["--repo", str(repo), "init", "--no-branch", "--name", "Game", "--priority", "3"]) == 0
    [p] = projects.discover(tmp_path / "home")
    assert p.repo == repo.resolve() and p.name == "Game" and p.priority == 3


def test_cli_drafts_get_subscription_budgets(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "r", {"go.mod": "module x\n"})
    monkeypatch.setattr(setup_agent, "draft_models", lambda: {"provider": "claude_code"})
    assert setup_agent.detect(repo)["budgets"]["monthly_cap_usd"] == 100000.0
    monkeypatch.setattr(setup_agent, "draft_models", lambda: {"provider": "anthropic"})
    assert "budgets" not in setup_agent.detect(repo)
