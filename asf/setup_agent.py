"""Setup agent: read a repository and draft its factory.yaml.

Detection is deterministic so the same repo always yields the same draft. The
draft is meant to be reviewed as a pull request, which is the team's first
interaction with the factory.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from .util import git, run

SKIP = {"node_modules", ".git", "dist", "build", ".venv", "venv", "vendor", "target", ".expo",
        "android", "ios", "__pycache__", ".next", "coverage"}
SENSITIVE = {
    3: ["auth", "login", "session", "oauth", "clerk", "security", "crypto", "payment", "payments",
        "billing", "stripe", "checkout", "entitlement", "entitlements", "migration", "migrations",
        "infra", "terraform", "k8s", "helm", "deploy", "privacy", "admin", "permissions", "secrets"],
}
SENSITIVE_FILES = ["Dockerfile", "docker-compose*.yml", "**/*.sql", "drizzle.config.*", "prisma/schema.prisma",
                   "eas.json", "app.json", "railway.json", "vercel.json", "fly.toml", "render.yaml",
                   "netlify.toml", "**/AndroidManifest.xml", "**/Info.plist", "**/*.entitlements",
                   "**/PrivacyInfo.xcprivacy", ".replit"]


def _tracked(repo: Path) -> list[str]:
    return [f for f in git(repo, "ls-files").splitlines() if f]


def _json(path: Path) -> dict[str, Any]:
    try:
        text = re.sub(r"^\s*//.*$", "", path.read_text(), flags=re.M)  # tolerate line comments
        return json.loads(text)
    except (OSError, json.JSONDecodeError):
        return {}


def default_branch(repo: Path) -> str:
    res = run(["git", "-C", str(repo), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], shell=False)
    if res.ok and "/" in res.stdout:
        return res.stdout.strip().split("/", 1)[1]
    for name in ("main", "master", "trunk", "develop"):
        if run(["git", "-C", str(repo), "rev-parse", "--verify", f"refs/heads/{name}"], shell=False).ok:
            return name
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()


def node_runner(pkg_dir: Path, repo: Path) -> str:
    for d in (pkg_dir, repo):
        if (d / "pnpm-lock.yaml").exists():
            return "pnpm"
        if (d / "yarn.lock").exists():
            return "yarn"
        if (d / "bun.lockb").exists() or (d / "bun.lock").exists():
            return "bun"
    return "npm"


def _exec_prefix(runner: str) -> str:
    return {"pnpm": "pnpm exec", "yarn": "yarn", "bun": "bunx", "npm": "npx"}[runner]


def _script_cmd(runner: str, name: str) -> str:
    return {"npm": f"npm run -s {name}", "pnpm": f"pnpm run -s {name}", "yarn": f"yarn -s {name}",
            "bun": f"bun run {name}"}[runner]


DIRECT_TOOLS = ("vitest", "jest", "mocha", "ava", "playwright", "tsc", "eslint", "biome", "next", "vite")


def _direct(script: str, runner: str, with_args: bool) -> str | None:
    """Turn `vitest run --config x` into `npx vitest run --config x {args}` so agents can target one file."""
    s = script.strip()
    if any(op in s for op in ("&&", "||", ";", "|")) or s.startswith(("NODE_ENV", "cross-env")):
        return None
    if s.split()[0] in DIRECT_TOOLS:
        return f"{_exec_prefix(runner)} {s}" + (" {args}" if with_args else "")
    return None


def node_package(repo: Path, rel: str, name: str, others: list[str]) -> list[dict[str, Any]]:
    pkg_dir = repo / rel
    data = _json(pkg_dir / "package.json")
    scripts: dict[str, str] = data.get("scripts", {})
    runner = node_runner(pkg_dir, repo)
    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    prefix = "" if rel == "." else rel.rstrip("/") + "/"
    ignore = [f"{o}/**" for o in others if rel == "."]
    tsconfig = _json(pkg_dir / "tsconfig.json") if (pkg_dir / "tsconfig.json").exists() else None
    ts_triggers = [prefix + g.replace("/**/*", "/**") for g in (tsconfig or {}).get("include", [])]
    base_triggers = ts_triggers or [f"{prefix}**"]

    common: dict[str, str] = {}
    for kind, names in (("lint", ["lint"]), ("typecheck", ["typecheck", "type-check", "check", "tsc"]),
                        ("build", ["build"])):
        for n in names:
            if n in scripts and not scripts[n].startswith(("expo ", "eas ")):
                common[kind] = _direct(scripts[n], runner, False) or _script_cmd(runner, n)
                break
    if "typecheck" not in common and tsconfig is not None and "typescript" in deps:
        common["typecheck"] = f"{_exec_prefix(runner)} tsc --noEmit -p ."

    test_script = scripts.get("test", "")
    parts = re.findall(r"(?:npm|pnpm|yarn)\s+(?:run\s+)?(test:[\w:-]+)", test_script)
    packages: list[dict[str, Any]] = []
    if parts and all(p in scripts for p in parts):
        # Composite test script: one package per part so gates run only where the diff lands.
        shared = [f"{prefix}{d}/**" for d in ("shared", "common", "lib") if (pkg_dir / d).is_dir()]
        if common:
            packages.append({"name": name or "app", "root": rel, "triggers": base_triggers,
                             "ignore": ignore, "commands": dict(common)})
        for part in parts:
            area = part.split(":", 1)[1]
            triggers = [f"{prefix}{area}/**", *shared] if (pkg_dir / area).is_dir() else base_triggers
            packages.append({"name": f"{name}-{area}" if name else area, "root": rel, "triggers": triggers,
                             "ignore": ignore,
                             "commands": {"test": _direct(scripts[part], runner, True) or _script_cmd(runner, part)}})
        return packages
    cmds = dict(common)
    if test_script and "no test specified" not in test_script:
        cmds["test"] = _direct(test_script, runner, True) or _script_cmd(runner, "test")
    if cmds:
        packages.append({"name": name or "app", "root": rel, "triggers": base_triggers,
                         "ignore": ignore, "commands": cmds})
    return packages


def python_package(repo: Path, rel: str, name: str, others: list[str]) -> list[dict[str, Any]]:
    d = repo / rel
    text = "".join((d / f).read_text() for f in ("pyproject.toml", "requirements.txt", "requirements-dev.txt",
                                                 "setup.cfg") if (d / f).exists())
    py = ".venv/bin/python" if (d / ".venv" / "bin" / "python").exists() else "python3"
    cmds: dict[str, str] = {}
    if "pytest" in text or (d / "tests").is_dir() or (d / "pytest.ini").exists():
        cmds["test"] = f"{py} -m pytest -q {{args}}"
    if "ruff" in text or (d / "ruff.toml").exists():
        cmds["lint"] = f"{py} -m ruff check ."
    if "mypy" in text:
        cmds["typecheck"] = f"{py} -m mypy ."
    prefix = "" if rel == "." else rel.rstrip("/") + "/"
    return [{"name": name or "app", "root": rel, "triggers": [f"{prefix}**"],
             "ignore": [f"{o}/**" for o in others if rel == "."], "commands": cmds}] if cmds else []


def simple_package(kind: str, repo: Path, rel: str, name: str, others: list[str]) -> list[dict[str, Any]]:
    prefix = "" if rel == "." else rel.rstrip("/") + "/"
    cmds = {
        "go": {"test": "go test ./... {args}", "lint": "go vet ./...", "build": "go build ./..."},
        "rust": {"test": "cargo test {args}", "lint": "cargo clippy -- -D warnings", "build": "cargo build"},
        "maven": {"test": "mvn -q test {args}", "build": "mvn -q -DskipTests package"},
        "gradle": {"test": "./gradlew test {args}", "build": "./gradlew assemble"},
        "ruby": {"test": "bundle exec rspec {args}" if (repo / rel / "spec").is_dir() else "bundle exec rake test"},
    }[kind]
    return [{"name": name or kind, "root": rel, "triggers": [f"{prefix}**"],
             "ignore": [f"{o}/**" for o in others if rel == "."], "commands": cmds}]


MANIFESTS = [("package.json", "node"), ("pyproject.toml", "python"), ("requirements.txt", "python"),
             ("setup.py", "python"), ("go.mod", "go"), ("Cargo.toml", "rust"), ("pom.xml", "maven"),
             ("build.gradle", "gradle"), ("build.gradle.kts", "gradle"), ("Gemfile", "ruby")]


def find_units(repo: Path, files: list[str]) -> list[tuple[str, str]]:
    units: dict[str, str] = {}
    for f in files:
        parts = f.split("/")
        if len(parts) > 3 or any(p in SKIP for p in parts[:-1]):
            continue
        for manifest, kind in MANIFESTS:
            if parts[-1] == manifest:
                rel = "/".join(parts[:-1]) or "."
                units.setdefault(rel, kind)
    return sorted(units.items(), key=lambda x: (x[0] != ".", x[0]))


def detect(repo: Path) -> dict[str, Any]:
    repo = repo.resolve()
    files = _tracked(repo)
    units = find_units(repo, files)
    subdirs = [rel for rel, _ in units if rel != "."]
    packages: list[dict[str, Any]] = []
    stacks = set()
    for rel, kind in units:
        name = "" if rel == "." else rel.replace("/", "-")
        others = [s for s in subdirs if s != rel]
        stacks.add(kind)
        if kind == "node":
            packages += node_package(repo, rel, name, others)
        elif kind == "python":
            packages += python_package(repo, rel, name, others)
        else:
            packages += simple_package(kind, repo, rel, name, others)

    clone_dirs = []
    for rel, kind in units:
        prefix = "" if rel == "." else rel + "/"
        for dep_dir in ("node_modules", ".venv"):
            if (repo / prefix / dep_dir).is_dir():
                clone_dirs.append(prefix + dep_dir)
    for f in ("client", "web", "app", "frontend"):
        if (repo / f / "node_modules").is_dir() and f"{f}/node_modules" not in clone_dirs:
            clone_dirs.append(f"{f}/node_modules")

    # Risk tiers from the paths that actually exist.
    dirs = sorted({"/".join(f.split("/")[:i]) for f in files for i in range(1, min(len(f.split("/")), 4))})
    tier3 = set()
    for d in dirs:
        last = d.rsplit("/", 1)[-1].lower()
        if last in SENSITIVE[3] and not any(p in SKIP for p in d.split("/")):
            tier3.add(f"{d}/**")
    stem_hits = {f for f in files if Path(f).stem.lower() in SENSITIVE[3] and Path(f).suffix in
                 (".ts", ".tsx", ".js", ".py", ".go", ".rb", ".java", ".kt")}
    tier3 |= stem_hits
    tier3 |= {g for g in SENSITIVE_FILES if any(_glob_hit(f, g) for f in files)}
    tier3 |= {".github/**", "factory.yaml"}
    for native in ("android", "ios", "mobile/android", "mobile/ios"):
        if (repo / native).is_dir():
            tier3.add(f"{native}/**")

    intake: dict[str, Any] = {"local": {"enabled": True}}
    if (repo / ".beads").is_dir():
        intake["beads"] = {"enabled": True, "exclude_types": ["epic"], "max_priority": 3, "write_back": False}
    remote = run(["git", "-C", str(repo), "remote", "get-url", "origin"], shell=False).stdout.strip()
    if "github.com" in remote:
        intake["github_issues"] = {"enabled": False, "label": "factory", "write_back": False}

    deploy = [n for n in ("vercel.json", "fly.toml", "render.yaml", "railway.json", "netlify.toml",
                          "eas.json", "mobile/eas.json", "app.yaml", "Procfile", ".replit") if (repo / n).exists()]
    instruction_files = [n for n in ("CLAUDE.md", "AGENTS.md", "CONTRIBUTING.md", ".cursorrules",
                                     ".github/copilot-instructions.md", "README.md") if (repo / n).exists()]
    ci = [n for n in (".github/workflows", ".gitlab-ci.yml", ".circleci", "bitbucket-pipelines.yml")
          if (repo / n).exists()]
    return {
        "version": 1,
        "project": {"name": repo.name, "default_branch": default_branch(repo),
                    "stacks": sorted(stacks), "deploy_targets": deploy, "ci": ci,
                    "remote": re.sub(r"https://[^@/]+@", "https://", remote)},
        "packages": packages,
        "intake": intake,
        "execution": {"provider": "local_worktree", "clone_dirs": clone_dirs, "setup": []},
        "tiers": {"rules": [
            {"paths": ["docs/**", "**/*.md", "CHANGELOG*", "LICENSE*"], "tier": 0},
            {"paths": ["**/__tests__/**", "**/*.test.*", "**/*.spec.*", "tests/**", "**/test_*.py"], "tier": 1},
            {"paths": sorted(tier3), "tier": 3},
        ], "classifier_default": 2},
        "context": {"instruction_files": instruction_files},
        "delivery": {"provider": "local_git"},
        "autonomy": {"shadow": True, "max_merge_tier": -1},
    }


def _glob_hit(path: str, pattern: str) -> bool:
    from .util import glob_match
    return glob_match(path, pattern)


HEADER = """# factory.yaml - drafted by `asf init` on {date}. Review it like any other pull request.
#
# Every change to this file moves the trust boundary, so it is itself protected:
# agents cannot edit it, and changing it is a human-reviewed diff.
#
# Detected: {stacks}. Deploy targets: {deploy}. CI: {ci}.
# Unset keys fall back to safe defaults (see asf/config.py DEFAULTS).
"""

SECTION_NOTES = {
    "packages": "Each package has its own commands. Gates run only for packages whose triggers match\n"
                "# the diff. {args} lets agents run a single test file.",
    "intake": "Where work comes from. Enable only what the team already uses.",
    "execution": "local_worktree: git worktree + scrubbed env (laptop or CI runner).\n"
                 "# docker: same, but commands run in a container with no network.",
    "tiers": "Path rules win over the triage classifier; a `tier:N` label overrides both.",
    "autonomy": "Shadow mode: agents open draft branches/PRs and nothing merges.\n"
                "# Raise max_merge_tier (in a reviewed PR) once the shadow report looks good.",
}


class _NoAlias(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


def render(cfg: dict[str, Any]) -> str:
    from datetime import date
    p = cfg["project"]
    out = [HEADER.format(date=date.today().isoformat(), stacks=", ".join(p.get("stacks", [])) or "none",
                         deploy=", ".join(p.get("deploy_targets", [])) or "none",
                         ci=", ".join(p.get("ci", [])) or "none")]
    for key, value in cfg.items():
        note = SECTION_NOTES.get(key)
        if note:
            out.append(f"\n# {note}")
        out.append(yaml.dump({key: value}, Dumper=_NoAlias, sort_keys=False, width=110).rstrip())
    return "\n".join(out) + "\n"


def write_branch(repo: Path, text: str, branch: str, state_dir: Path) -> str:
    """Commit factory.yaml on a new branch through a temporary worktree.

    The user's checkout, index and working tree are never touched.
    """
    base = default_branch(repo)
    wt = state_dir / "setup-worktree"
    if wt.exists():
        git(repo, "worktree", "remove", "--force", str(wt), check=False)
    exists = run(["git", "-C", str(repo), "rev-parse", "--verify", f"refs/heads/{branch}"], shell=False).ok
    git(repo, "worktree", "add", str(wt), branch) if exists else \
        git(repo, "worktree", "add", "-b", branch, str(wt), base)
    try:
        (wt / "factory.yaml").write_text(text)
        git(wt, "add", "factory.yaml")
        env = {"GIT_AUTHOR_NAME": "Software Factory", "GIT_AUTHOR_EMAIL": "factory@asf.local",
               "GIT_COMMITTER_NAME": "Software Factory", "GIT_COMMITTER_EMAIL": "factory@asf.local"}
        import os
        subprocess.run(["git", "-C", str(wt), "commit", "-q", "-m",
                        "Add factory.yaml drafted by the setup agent"], env={**os.environ, **env}, check=False)
        return git(wt, "rev-parse", "HEAD").strip()
    finally:
        git(repo, "worktree", "remove", "--force", str(wt), check=False)
