"""Guided setup: check this machine, add a project, check the project.

Shared by `asf setup` (terminal wizard or flags) and the console's Set up page, so both give
the same answers. Nothing here spends model time: sign-in is read from each CLI's own status
command, never by making a model call.

The minimum to run the factory is Python 3.11+, git, and one signed-in model backend
(Claude Code, Cursor, or an Anthropic API key). Everything else is optional and only shown
as a hint with the command that adds it.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from . import config as config_mod
from . import setup_agent
from .projects import SEARCH_ROOTS, asf_home, discover, record, set_entry
from .util import new_id, run, sha

CLAUDE_INSTALL = "curl -fsSL https://claude.ai/install.sh | bash"
CURSOR_INSTALL = "curl https://cursor.com/install -fsS | bash"
BACKENDS = ("auto", "claude", "cursor", "pool", "anthropic")


@dataclass
class Check:
    id: str
    label: str
    status: str            # ok | warn | fail | info
    detail: str = ""
    fix: str = ""          # a command to run, or a short instruction
    required: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def code_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _venv_bin() -> Path:
    return Path(sys.executable).parent


# ----------------------------------------------------------------------------- machine

def _cli(name: str) -> str | None:
    if name == "claude":
        from .adapters.models.claude_code import find_binary
    else:
        from .adapters.models.cursor_cli import find_binary
    return find_binary({})


def claude_status(binary: str) -> tuple[bool, str]:
    """(signed in, how). Reads `claude auth status`; no model call, nothing personal returned."""
    res = run([binary, "auth", "status", "--json"], shell=False, timeout=20)
    try:
        d = json.loads(res.stdout or "{}")
    except json.JSONDecodeError:
        return False, (res.stderr or res.stdout).strip()[:200]
    if d.get("loggedIn"):
        return True, f"signed in ({d.get('authMethod') or 'unknown method'})"
    return False, "not signed in"


def cursor_status(binary: str) -> tuple[bool, str]:
    res = run([binary, "status"], shell=False, timeout=20)
    text = (res.stdout + res.stderr).lower()
    if res.ok and "logged in" in text and "not logged in" not in text:
        return True, "signed in"
    return False, "not signed in"


def machine_checks(env: dict[str, str] | None = None) -> list[Check]:
    env = dict(os.environ if env is None else env)
    out: list[Check] = []
    v = sys.version_info
    out.append(Check("python", "Python 3.11 or newer", "ok" if v >= (3, 11) else "fail",
                     f"{v.major}.{v.minor}.{v.micro} at {sys.executable}",
                     "" if v >= (3, 11) else "install Python 3.11+ and run scripts/install.sh again", True))
    git_bin = shutil.which("git")
    git_ver = run(["git", "--version"], shell=False).stdout.strip() if git_bin else ""
    out.append(Check("git", "git", "ok" if git_bin else "fail", git_ver or "not found",
                     "" if git_bin else "install git (macOS: xcode-select --install)", True))

    # Model backends: any one is enough.
    ready = []
    claude = _cli("claude")
    if claude:
        ok, how = claude_status(claude)
        out.append(Check("claude", "Claude Code CLI", "ok" if ok else "warn", f"{claude}: {how}",
                         "" if ok else "claude auth login"))
        if ok:
            ready.append("Claude Code")
    else:
        out.append(Check("claude", "Claude Code CLI", "info", "not installed (recommended backend)",
                         CLAUDE_INSTALL))
    cursor = _cli("cursor")
    if cursor:
        ok, how = cursor_status(cursor)
        out.append(Check("cursor", "Cursor CLI", "ok" if ok else "warn", f"{cursor}: {how}",
                         "" if ok else "cursor-agent login"))
        if ok:
            ready.append("Cursor")
    else:
        out.append(Check("cursor", "Cursor CLI", "info", "not installed (optional second backend)",
                         CURSOR_INSTALL))
    if env.get("ANTHROPIC_API_KEY"):
        out.append(Check("api_key", "Anthropic API key", "ok", "ANTHROPIC_API_KEY is set (value not shown)"))
        ready.append("Anthropic API")
    else:
        out.append(Check("api_key", "Anthropic API key", "info",
                         "not set; only needed without a signed-in CLI", "export ANTHROPIC_API_KEY=..."))
    out.append(Check("backend", "A model backend agents can use", "ok" if ready else "fail",
                     ", ".join(ready) if ready else "no signed-in CLI and no API key",
                     "" if ready else "claude auth login   (or cursor-agent login, or set ANTHROPIC_API_KEY)",
                     True))

    home = asf_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
        writable = os.access(home, os.W_OK)
    except OSError:
        writable = False
    out.append(Check("asf_home", "Factory state folder", "ok" if writable else "fail", str(home),
                     "" if writable else f"make {home} writable, or set ASF_HOME", True))

    # Convenience and optional features.
    on_path = shutil.which("asf", path=env.get("PATH"))
    link = f"mkdir -p ~/.local/bin && ln -sf {_venv_bin() / 'asf'} ~/.local/bin/asf"
    out.append(Check("asf_path", "asf on PATH", "ok" if on_path else "warn",
                     on_path or "not on PATH; call it by its full path or link it", "" if on_path else link))
    npx = shutil.which("npx", path=env.get("PATH"))
    out.append(Check("node", "Node.js (browser reproductions)", "ok" if npx else "info",
                     npx or "not found; only needed for proving UI bugs in a browser",
                     "" if npx else "install Node.js 20+ (https://nodejs.org)"))
    try:
        import importlib.util
        has_pw = importlib.util.find_spec("playwright") is not None
    except (ImportError, ValueError):
        has_pw = False
    out.append(Check("playwright", "Playwright (e2e tests)", "ok" if has_pw else "info",
                     "installed" if has_pw else "not installed; only needed for e2e suites",
                     "" if has_pw else f"scripts/install.sh --with-e2e"))
    gh = shutil.which("gh", path=env.get("PATH"))
    if gh:
        authed = run([gh, "auth", "status"], shell=False, timeout=20).ok
        out.append(Check("gh", "GitHub CLI (issues and pull requests)", "ok" if authed else "warn",
                         "signed in" if authed else "installed, not signed in", "" if authed else "gh auth login"))
    else:
        out.append(Check("gh", "GitHub CLI (issues and pull requests)", "info",
                         "not installed; only needed for GitHub intake or pull requests",
                         "brew install gh && gh auth login"))
    return out


def ready(checks: list[Check]) -> bool:
    return not any(c.required and c.status == "fail" for c in checks)


# ----------------------------------------------------------------------------- projects

def state_dir(repo: Path) -> Path:
    """Same naming as the CLI (`asf --repo PATH ...`), so both find one state folder."""
    repo = repo.expanduser().resolve()
    return asf_home() / f"{repo.name}-{sha(str(repo), 6)}"


def candidate_repos(roots: list[str] | None = None, limit: int = 60) -> list[dict[str, Any]]:
    """Git repositories one level under the usual code folders, registered ones marked."""
    known = {str(p.repo.resolve()): p.key for p in discover() if p.repo}
    seen, out = set(), []
    for root in roots or [r for r in SEARCH_ROOTS if r != "~"]:
        base = Path(root).expanduser()
        if not base.is_dir():
            continue
        try:
            children = sorted(base.iterdir())
        except OSError:
            continue
        for d in children:
            if d.name.startswith(".") or not (d / ".git").exists():
                continue
            path = str(d.resolve())
            if path in seen:
                continue
            seen.add(path)
            out.append({"path": path, "name": d.name, "project": known.get(path),
                        "has_config": (d / "factory.yaml").exists()})
            if len(out) >= limit:
                return out
    return out


def validate_repo(repo: Path) -> str | None:
    """Why this path cannot be a project, or None."""
    if not repo.is_dir():
        return f"{repo} is not a folder"
    if not run(["git", "-C", str(repo), "rev-parse", "--git-dir"], shell=False).ok:
        return f"{repo} is not a git repository (run `git init` and commit something first)"
    if not run(["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"], shell=False).ok:
        return f"{repo} has no commits yet; commit a README and one passing test first"
    return None


def _models_for(backend: str) -> tuple[dict[str, Any], list[str]]:
    """factory.yaml `models:` for a backend choice, and warnings when it is not usable here."""
    claude, cursor = _cli("claude"), _cli("cursor")
    warn: list[str] = []
    if backend == "auto":
        return setup_agent.draft_models(claude=claude, cursor=cursor), warn
    if backend == "claude":
        if not claude:
            warn.append(f"Claude Code is not installed here: {CLAUDE_INSTALL}")
        return setup_agent.draft_models(claude=claude or "claude", cursor=None), warn
    if backend == "cursor":
        if not cursor:
            warn.append(f"Cursor CLI is not installed here: {CURSOR_INSTALL}")
        return setup_agent.draft_models(claude=None, cursor=cursor or "cursor-agent"), warn
    if backend == "pool":
        for name, found, how in (("Claude Code", claude, CLAUDE_INSTALL), ("Cursor CLI", cursor, CURSOR_INSTALL)):
            if not found:
                warn.append(f"{name} is not installed here: {how}")
        return setup_agent.draft_models(claude=claude or "claude", cursor=cursor or "cursor-agent"), warn
    if backend == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            warn.append("ANTHROPIC_API_KEY is not set; pass --env-file to asf commands or export it")
        return {"provider": "anthropic"}, warn
    raise ValueError(f"backend must be one of {', '.join(BACKENDS)}")


def draft(repo: Path | str, options: dict[str, Any] | None = None,
          detect: Callable[[Path], dict[str, Any]] = setup_agent.detect) -> dict[str, Any]:
    """Draft factory.yaml for a repository with the chosen options. Writes nothing."""
    opts = dict(options or {})
    repo = Path(repo).expanduser().resolve()
    problem = validate_repo(repo)
    if problem:
        raise ValueError(problem)
    cfg = detect(repo)
    warnings: list[str] = []
    tips: list[str] = []

    models, w = _models_for(opts.get("backend") or "auto")
    warnings += w
    cfg["models"] = models
    if models.get("provider") == "anthropic":
        cfg.pop("budgets", None)
    else:
        cfg["budgets"] = setup_agent.CLI_BUDGETS

    remote = cfg["project"].get("remote", "")
    if opts.get("github_issues"):
        cfg["intake"]["github_issues"] = {"enabled": True, "label": opts.get("label") or "factory",
                                          "write_back": bool(opts.get("write_back"))}
        if "github.com" not in remote:
            warnings.append("GitHub issues intake is on, but origin is not a GitHub remote")
    if opts.get("github_prs"):
        cfg["delivery"] = {"provider": "github", "labels": ["factory"]}
        if "github.com" not in remote:
            warnings.append("GitHub pull requests are on, but origin is not a GitHub remote")
        if not shutil.which("gh"):
            warnings.append("GitHub pull requests need the GitHub CLI: brew install gh && gh auth login")
    if opts.get("name"):
        cfg["project"]["name"] = str(opts["name"])

    text = setup_agent.render(cfg)
    config_mod.load(text=text)  # never offer a draft the factory would refuse

    pkgs = cfg.get("packages") or []
    if not pkgs:
        warnings.append("No package was detected (no package.json, pyproject.toml, go.mod, ...). Add one "
                        "under `packages:` with a test command before running jobs.")
    untested = [p.get("name") or "(root)" for p in pkgs if "test" not in (p.get("commands") or {})]
    if pkgs and untested:
        warnings.append(f"No test command for: {', '.join(untested)}. Gates cannot verify changes there.")
    if not cfg.get("context", {}).get("instruction_files"):
        tips.append("Add a CLAUDE.md or AGENTS.md at the root: agents read it before every job.")
    if not cfg.get("execution", {}).get("clone_dirs") and "node" in cfg["project"].get("stacks", []):
        tips.append("node_modules was not found; run your install once so sandboxes can reuse it.")
    in_repo = (repo / "factory.yaml").exists()
    if in_repo:
        tips.append("This repository already has factory.yaml; it is used instead of this draft.")
    tier3 = next((r["paths"] for r in cfg["tiers"]["rules"] if r.get("tier") == 3), [])
    roles = models.get("roles") or {}
    if models.get("provider") == "pool":
        backend_line = "pool: " + ", ".join(f"{k} ({v['provider']})" for k, v in models["backends"].items())
    else:
        backend_line = models.get("provider", "")
    return {
        "repo": str(repo), "key": state_dir(repo).name, "yaml": text, "has_repo_config": in_repo,
        "summary": {
            "name": cfg["project"]["name"], "default_branch": cfg["project"].get("default_branch"),
            "stacks": cfg["project"].get("stacks", []), "remote": remote,
            "packages": [{"name": p.get("name") or "(root)", "root": p.get("root", "."),
                          "commands": p.get("commands", {})} for p in pkgs],
            "owner_only_paths": tier3, "instruction_files": cfg.get("context", {}).get("instruction_files", []),
            "intake": sorted(k for k, v in cfg["intake"].items() if v.get("enabled")),
            "delivery": cfg["delivery"]["provider"], "backend": backend_line,
            "roles": {k: v.get("model") for k, v in roles.items()},
            "shadow": True,
        },
        "warnings": warnings, "tips": tips,
    }


def create(repo: Path | str, options: dict[str, Any] | None = None,
           detect: Callable[[Path], dict[str, Any]] = setup_agent.detect) -> dict[str, Any]:
    """Add a project: write the pending config, register it, commit the draft on a branch."""
    opts = dict(options or {})
    d = draft(repo, opts, detect=detect)
    repo = Path(d["repo"])
    sd = state_dir(repo)
    sd.mkdir(parents=True, exist_ok=True)
    pending = sd / "factory.yaml"
    notes: list[str] = []
    wrote = False
    if d["has_repo_config"]:
        notes.append("kept the repository's own factory.yaml")
    elif pending.exists() and not opts.get("force"):
        notes.append(f"kept the existing draft at {pending} (choose 'replace' to overwrite it)")
    else:
        pending.write_text(d["yaml"])
        wrote = True
        notes.append(f"drafted {pending}")

    from .store import Store
    Store(sd / "factory.db")
    record(sd, repo)
    entry = {k: v for k, v in (("name", opts.get("name")), ("priority", opts.get("priority")))
             if v not in (None, "")}
    if "priority" in entry:
        entry["priority"] = int(entry["priority"])
    if entry:
        set_entry(sd.name, **entry)
    notes.append(f"registered as {sd.name}")

    branch = opts.get("branch_name") or "factory/setup"
    commit = ""
    if wrote and opts.get("branch", True):
        try:
            commit = setup_agent.write_branch(repo, d["yaml"], branch, sd)
            notes.append(f"committed the draft on branch {branch} ({commit[:10]}); your checkout was not touched")
        except Exception as e:  # noqa: BLE001 - the project works from the pending copy anyway
            d["warnings"].append(f"could not commit the draft on {branch}: {str(e)[:300]}")
    checks = project_checks(repo, sd)
    return {**d, "key": sd.name, "state_dir": str(sd), "notes": notes, "branch": branch if commit else "",
            "commit": commit, "checks": [c.as_dict() for c in checks], "healthy": ready(checks)}


def project_checks(repo: Path, sd: Path | None = None,
                   secret: Callable[[str], str | None] | None = None,
                   cfg: config_mod.Config | None = None) -> list[Check]:
    """What `asf doctor` checks, as data."""
    from .adapters import base as adapters
    repo = Path(repo).expanduser().resolve()
    sd = sd or state_dir(repo)
    secret = secret or (lambda n: os.environ.get(n))
    out: list[Check] = []
    try:
        cfg = cfg or config_mod.resolve(repo, None, sd)
    except config_mod.ConfigError as e:
        return [Check("config", "factory.yaml", "fail", str(e), f"asf --repo {repo} setup add", True)]
    where = "the repository" if Path(cfg.source) == repo / "factory.yaml" else "the pending draft"
    out.append(Check("config", "factory.yaml", "ok", f"{cfg.source} ({where}, policy {cfg.version_hash})",
                     "" if where == "the repository" else
                     f"merge branch factory/setup in {repo} to make it official", True))
    is_git = (repo / ".git").exists()
    out.append(Check("git", "git repository", "ok" if is_git else "fail", str(repo), "", True))
    has_branch = run(["git", "-C", str(repo), "rev-parse", "--verify", cfg.default_branch], shell=False).ok
    out.append(Check("branch", f"default branch {cfg.default_branch}", "ok" if has_branch else "fail",
                     "exists" if has_branch else "not found",
                     "" if has_branch else "set project.default_branch in factory.yaml", True))
    tests = 0
    for p in cfg.packages:
        for kind, cmd in p.commands.items():
            if kind == "repro":
                continue
            tests += kind == "test"
            exe = cmd.split()[0]
            found = shutil.which(exe) or (repo / p.root / exe).exists()
            out.append(Check(f"cmd:{p.name}:{kind}", f"package {p.name}: {kind}", "ok" if found else "fail",
                             cmd if found else f"'{exe}' not on PATH: {cmd}",
                             "" if found else "use an absolute path in factory.yaml (sandboxes get a minimal PATH)",
                             True))
    if not tests:
        out.append(Check("tests", "a test command", "warn", "no package has a test command",
                         "add packages[].commands.test to factory.yaml"))
    for d in cfg.get("execution.clone_dirs", []):
        present = (repo / d).exists()
        out.append(Check(f"clone:{d}", f"dependency folder {d}", "ok" if present else "warn",
                         "present" if present else "missing", "" if present else "run your install once"))
    for kind in (cfg.get("intake") or {}):
        try:
            adapters.load("intake", kind)
            out.append(Check(f"intake:{kind}", f"intake {kind}", "ok", "adapter available"))
        except KeyError as e:
            out.append(Check(f"intake:{kind}", f"intake {kind}", "fail", str(e), "", True))
    for cat, key in (("sandbox", "execution.provider"), ("delivery", "delivery.provider"),
                     ("models", "models.provider")):
        try:
            adapters.load(cat, cfg.get(key))
            out.append(Check(f"adapter:{cat}", f"{cat} adapter {cfg.get(key)}", "ok", "available"))
        except KeyError as e:
            out.append(Check(f"adapter:{cat}", f"{cat} adapter", "fail", str(e), "", True))
    provider = cfg.get("models.provider")
    if provider == "anthropic":
        key_env = cfg.get("models.api_key_env", "ANTHROPIC_API_KEY")
        has = bool(secret(key_env))
        out.append(Check("models", f"{key_env}", "ok" if has else "fail",
                         "available (value not shown)" if has else "not set",
                         "" if has else "pass --env-file or export it", True))
    elif provider == "pool":
        from .adapters.models.pool import PoolProvider
        pool = PoolProvider(cfg.get("models") or {}, None)
        any_ready = False
        for name, prov in pool.backends.items():
            problem = prov.credentials_problem()
            any_ready = any_ready or not problem
            out.append(Check(f"backend:{name}", f"backend {name} ({prov.kind})", "warn" if problem else "ok",
                             problem or "ready"))
        if not any_ready:
            out.append(Check("models", "a usable backend", "fail", "no backend in the pool is ready", "", True))
        for role in ("triage", "implementer", "tester", "reviewer", "security", "ci_fixer"):
            out.append(Check(f"route:{role}", f"{role}", "info", " -> ".join(pool.order(role))))
    elif provider == "claude_code":
        from .adapters.models.claude_code import find_binary
        b = find_binary(cfg.get("models") or {})
        out.append(Check("models", "Claude Code CLI", "ok" if b else "fail", b or "not found",
                         "" if b else "set models.binary or ASF_CLAUDE_BIN", True))
    elif provider == "cursor_cli":
        from .adapters.models.cursor_cli import find_binary as find_cursor
        b = find_cursor(cfg.get("models") or {})
        out.append(Check("models", "Cursor CLI", "ok" if b else "fail", b or "not found",
                         "" if b else CURSOR_INSTALL, True))
    for st in cfg.get("deploy.steps") or []:
        when = ", ".join(st.get("when") or []) or "every merge"
        out.append(Check(f"deploy:{st['name']}", f"deploy {st['name']}", "info",
                         f"{st.get('approval', 'owner')} approval, when {when}"))
    out.append(Check("autonomy", "autonomy", "info",
                     f"shadow mode {'on' if cfg.get('autonomy.shadow') else 'off'}, max merge tier "
                     f"{cfg.get('autonomy.max_merge_tier')}, monthly cap ${cfg.get('budgets.monthly_cap_usd')}"))
    return out


def first_item(factory: Any, title: str, body: str = "", acceptance: list[str] | None = None,
               kind: str = "task") -> str:
    """Queue a work item by hand, as `asf add` does. Returns the job id."""
    from .models import WorkItem
    title = (title or "").strip()
    if not title:
        raise ValueError("a work item needs a title")
    if kind not in ("bug", "feature", "task", "chore"):
        raise ValueError("kind must be bug, feature, task or chore")
    item = WorkItem(source_kind="local", external_id=new_id("item"), title=title,
                    body=body or "", kind=kind,
                    acceptance=[a.strip() for a in (acceptance or []) if a and a.strip()])
    job_id, _ = factory.submit(item)
    return job_id
