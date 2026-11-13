"""Browser reproductions: English steps an agent runs in a real browser, and the Playwright test
it writes from them.

Two suites, both committed:
- `tests/repro/<slug>.md`: the English reproduction, a human-readable record. Front matter names
  the work item, the paths it `covers:` and the app path to open; the body has numbered steps,
  the expected result and the values to measure. The reproducer agent runs it (model time).
- `tests/e2e/test_<slug>.py`: the pytest-playwright test the reproducer wrote from it. A cheap,
  deterministic regression check, run as the `e2e` gate on every change and after merges.

The reproducer is `claude -p` with every built-in tool switched off (`--tools ""`) and one MCP
server, @playwright/mcp, pointed at the app the factory started. It can drive a browser and
nothing else: no files, no shell, no network beyond the app. It answers with structured output
(`--json-schema`); the factory writes the files. So the broker's trust boundary holds: the only
path from a model to the repository is still the factory's own code.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from string import Template
from typing import Any, Protocol
from urllib.parse import urlparse

import yaml

from .adapters.base import ModelTurn
from .config import Config, Package
from .util import any_match, find_cli, slugify

REPRO_SCHEMA = {
    "type": "object",
    "properties": {
        "reproduced": {"type": "boolean",
                       "description": "true when the problem shows (bug happens / behaviour missing)"},
        "measurements": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "value": {"type": "string"},
                           "expected": {"type": "string"}},
            "required": ["name", "value", "expected"], "additionalProperties": False}},
        "notes": {"type": "string"},
        "screenshots": {"type": "array", "items": {"type": "string"}},
        "test_source": {"type": "string", "description": "the pytest-playwright test, or empty"},
    },
    "required": ["reproduced", "measurements", "notes", "screenshots", "test_source"],
    "additionalProperties": False,
}

# The browser tools the reproducer may use. Everything else @playwright/mcp offers is refused,
# and the two that reach outside the browser (code run in the MCP server's own Node process, and
# uploads that read local files) are removed from the model's view outright.
SAFE_TOOLS = ["browser_navigate", "browser_navigate_back", "browser_click", "browser_type",
              "browser_press_key", "browser_hover", "browser_drag", "browser_drop",
              "browser_select_option", "browser_fill_form", "browser_snapshot", "browser_find",
              "browser_take_screenshot", "browser_evaluate", "browser_wait_for", "browser_resize",
              "browser_console_messages", "browser_network_requests", "browser_network_request",
              "browser_tabs", "browser_close", "browser_handle_dialog", "browser_emulate_media"]
UNSAFE_TOOLS = ["browser_run_code", "browser_run_code_unsafe", "browser_file_upload", "browser_install",
                "browser_pdf_save", "browser_start_tracing", "browser_stop_tracing"]
SERVER = "browser"


# ------------------------------------------------------------------------- the file --

@dataclass
class ReproSpec:
    path: str                    # repository-relative path of the .md file
    title: str = ""
    item: str = ""               # work item, e.g. github_issues:6
    kind: str = "bug"            # bug: the steps show a defect | feature: they show missing behaviour
    covers: list[str] = field(default_factory=list)
    url_path: str = "/"
    test: str = ""               # the generated test file, when known
    text: str = ""               # the whole file, as the agent reads it

    @property
    def slug(self) -> str:
        return Path(self.path).stem


def parse(text: str, path: str = "") -> ReproSpec:
    meta: dict[str, Any] = {}
    body = text
    if text.startswith("---"):
        _, fm, body = text.split("---", 2)
        meta = yaml.safe_load(fm) or {}
    title = next((ln.lstrip("# ").strip() for ln in body.splitlines() if ln.startswith("# ")), "")
    covers = meta.get("covers") or []
    return ReproSpec(path=path, title=title, item=str(meta.get("item", "")), kind=meta.get("kind", "bug"),
                     covers=[covers] if isinstance(covers, str) else list(covers),
                     url_path=str(meta.get("path") or "/"), test=str(meta.get("test", "")), text=text)


def render(title: str, item: str, kind: str, covers: list[str], url_path: str, steps: list[str],
           expected: str, measure: list[str], test: str = "") -> str:
    meta = {"item": item, "kind": kind, "covers": covers, "path": url_path}
    if test:
        meta["test"] = test
    numbered = "\n".join(f"{i}. {re.sub(r'^[0-9]+[.)] *', '', s).strip()}" for i, s in enumerate(steps, 1))
    measured = "\n".join(f"- {m.strip().lstrip('- ')}" for m in measure) or "- (nothing listed)"
    what = "bug" if kind == "bug" else "missing behaviour"
    return (f"---\n{yaml.safe_dump(meta, sort_keys=False).strip()}\n---\n# {title}\n\n"
            f"Reproduces the {what} in a real browser. Run by the factory's reproducer agent; "
            f"`{test or 'the e2e test'}` checks the same thing deterministically.\n\n"
            f"## Steps\n\n{numbered}\n\n## Expected\n\n{expected.strip()}\n\n"
            f"## Measure\n\n{measured}\n")


def slug_for(title: str) -> str:
    return slugify(title, 40)


def test_name(slug: str) -> str:
    return "test_" + slug.replace("-", "_") + ".py"


def suite_files(root: Path, cfg: Config) -> list[tuple[Package, ReproSpec]]:
    out = []
    for pkg, folder in cfg.repro_dirs():
        d = Path(root) / folder
        if d.is_dir():
            for f in sorted(d.glob("*.md")):
                if f.name.lower() == "readme.md":
                    continue
                out.append((pkg, parse(f.read_text(), str(f.relative_to(root)))))
    return out


def covering(root: Path, cfg: Config, changed: list[str], exclude: list[str] = ()) -> list[tuple[Package, ReproSpec]]:
    """Reproductions whose `covers:` globs match a changed file (the repro suite for a change)."""
    return [(p, s) for p, s in suite_files(root, cfg)
            if s.path not in exclude and s.covers and any(any_match(c, s.covers) for c in changed)]


def e2e_dir(cfg: Config, pkg: Package) -> str:
    root = "" if pkg.root in ("", ".") else pkg.root.rstrip("/") + "/"
    return root + str(cfg.get("repro.e2e_dir") or "tests/e2e").rstrip("/")


# ------------------------------------------------------------------------- the agent --

@dataclass
class ReproRequest:
    spec: ReproSpec
    url: str                     # where the app is running
    mode: str                    # reproduce | confirm | regenerate
    out_dir: Path                # screenshots go here
    test_path: str = ""          # repository path the test will be written to
    previous_test: str = ""      # regenerate: the test that did not fail by its assertion
    test_output: str = ""        # regenerate: why (pytest output and the failure kind)
    where: str = ""              # "the base commit" / "the fixed branch", for the prompt
    answers: list[dict] = field(default_factory=list)

    @property
    def want_test(self) -> bool:
        return self.mode in ("reproduce", "regenerate")


class Reproducer(Protocol):
    def reproduce(self, req: ReproRequest) -> tuple[dict[str, Any], ModelTurn]: ...


class ReproError(RuntimeError):
    pass


def _prompt(req: ReproRequest, project: str) -> tuple[str, str]:
    system = Template((files("asf.agents") / "prompts" / "reproducer.md").read_text()).safe_substitute(
        project=project, url=req.url)
    start = req.url.rstrip("/") + "/" + req.spec.url_path.lstrip("/")
    task = [f"<repro file=\"{req.spec.path}\">\n{req.spec.text.strip()}\n</repro>",
            f"The app is running at {req.url}; the reproduction starts at {start}. "
            f"The code under test is {req.where or 'the current code'}."]
    if req.answers:
        task.append("Clarifications from the requester:\n" + "\n".join(
            f"- Q: {a.get('question', '')}\n  A: {a.get('answer', '')}" for a in req.answers))
    if req.mode == "reproduce":
        task.append(f"Run the steps, measure, decide `reproduced`, and write the test for "
                    f"{req.test_path} in test_source.")
    elif req.mode == "regenerate":
        task.append("Your earlier test did not fail by its own assertion, so it does not prove anything "
                    f"yet. Fix it (run the steps again in the browser if you need to see the page):\n"
                    f"<previous_test>\n{req.previous_test}\n</previous_test>\n"
                    f"<test_output>\n{req.test_output[-6000:]}\n</test_output>\n"
                    f"Return the corrected test for {req.test_path} in test_source.")
    else:
        task.append("Run the steps and measure. This is a check only: leave test_source empty.")
    return system, "\n\n".join(task)


class ClaudeReproducer:
    """`claude -p`, built-in tools off, with @playwright/mcp as its only MCP server."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.settings = cfg.get("repro") or {}
        pooled = [b.get("binary") for b in (cfg.get("models.backends") or {}).values()
                  if isinstance(b, dict) and b.get("provider") == "claude_code" and b.get("binary")]
        explicit = self.settings.get("binary") or (cfg.get("models.binary") if cfg.get("models.provider") ==
                                                   "claude_code" else None) or next(iter(pooled), None)
        self.binary = find_cli("claude", explicit, "ASF_CLAUDE_BIN")

    def credentials_problem(self) -> str | None:
        return None if self.binary else "Claude Code CLI not found (repro.binary, ASF_CLAUDE_BIN or PATH)"

    def mcp_config(self, url: str, out_dir: Path) -> dict[str, Any]:
        cmd = list(self.settings.get("mcp_command") or ["npx", "-y", "@playwright/mcp@0.0.85"])
        origin = "{0.scheme}://{0.netloc}".format(urlparse(url))
        args = cmd[1:] + ["--headless", "--isolated", "--output-dir", str(out_dir),
                          "--allowed-origins", origin, "--viewport-size", "1280x800",
                          "--image-responses", "omit"]
        return {"mcpServers": {SERVER: {"command": cmd[0], "args": args}}}

    def command(self, system: str, mcp_path: Path) -> list[str]:
        s = self.settings
        allowed = ",".join(f"mcp__{SERVER}__{t}" for t in SAFE_TOOLS)
        denied = ",".join(f"mcp__{SERVER}__{t}" for t in UNSAFE_TOOLS)
        args = [self.binary or "claude", "-p", "--output-format", "json",
                "--tools", "",                                 # no built-in tools at all
                "--mcp-config", str(mcp_path), "--strict-mcp-config",   # and only this MCP server
                "--allowedTools", allowed, "--disallowedTools", denied,
                "--permission-mode", "dontAsk",                # anything not allowed is refused
                "--json-schema", json.dumps(REPRO_SCHEMA), "--system-prompt", system,
                "--model", str(s.get("model") or "claude-sonnet-5-5"),
                "--setting-sources", "", "--no-session-persistence"]
        if s.get("effort"):
            args += ["--effort", str(s["effort"])]
        return args

    def reproduce(self, req: ReproRequest) -> tuple[dict[str, Any], ModelTurn]:
        if not self.binary:
            raise ReproError(self.credentials_problem() or "no claude binary")
        req.out_dir.mkdir(parents=True, exist_ok=True)
        system, task = _prompt(req, self.cfg.get("project.name") or "")
        with tempfile.TemporaryDirectory(prefix="asf-repro-") as tmp:
            mcp_path = Path(tmp) / "mcp.json"
            mcp_path.write_text(json.dumps(self.mcp_config(req.url, req.out_dir)))
            env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
            timeout = int(self.settings.get("timeout_s") or 900)
            try:
                r = subprocess.run(self.command(system, mcp_path), input=task, capture_output=True, text=True,
                                   timeout=timeout, cwd=req.out_dir, env=env)
            except subprocess.TimeoutExpired as e:
                raise ReproError(f"[backend:claude] claude -p reproducer timed out after {timeout}s") from e
        try:
            d = json.loads(r.stdout)
        except json.JSONDecodeError as e:
            raise ReproError(f"[backend:claude] claude -p returned non-JSON (exit {r.returncode}): "
                             f"{(r.stderr or r.stdout)[:400]}") from e
        out = d.get("structured_output")
        if not isinstance(out, dict):
            raise ReproError(f"[backend:claude] claude -p reproducer gave no structured output: "
                             f"{str(d.get('result') or d.get('subtype'))[:400]}")
        u = d.get("usage") or {}
        turn = ModelTurn(text="", tool_calls=[], stop_reason="end_turn",
                         usage={"input": u.get("input_tokens", 0) or 0, "output": u.get("output_tokens", 0) or 0,
                                "cache_read": u.get("cache_read_input_tokens", 0) or 0,
                                "cache_write": u.get("cache_creation_input_tokens", 0) or 0},
                         cost_usd=float(d.get("total_cost_usd") or 0),
                         model="claude:" + next(iter(d.get("modelUsage") or {}), str(self.settings.get("model"))))
        out["turns"] = d.get("num_turns")
        return out, turn


def shots(out_dir: Path, base: Path) -> list[str]:
    """Screenshots the run left, as paths relative to the job's evidence folder."""
    if not out_dir.is_dir():
        return []
    found = [p for p in out_dir.rglob("*") if p.suffix.lower() in (".png", ".jpg", ".jpeg")]
    return [str(p.relative_to(base)) for p in sorted(found, key=lambda p: p.stat().st_mtime)]


def verdict(result: dict[str, Any] | None, kind: str = "bug") -> str:
    if not result:
        return "not run"
    if result.get("reproduced"):
        return "bug shows" if kind == "bug" else "behaviour missing"
    return "bug gone" if kind == "bug" else "behaviour present"


def describe(measurements: list[dict[str, Any]]) -> str:
    return "; ".join(f"{m.get('name')}: {m.get('value')}" for m in measurements)


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")
