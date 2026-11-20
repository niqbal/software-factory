"""The asf MCP server: verification any coding agent can call, with a verdict it cannot argue with.

An agent that has just written a change is the worst judge of whether it works. This server
exposes the factory's deterministic gates, and its cold-read reviewer, over a checkout the
agent has been editing:

    list_checks   what can be verified here, and what the diff touches (runs nothing)
    check_diff    the read-only gates: secrets, diff size, test integrity, dependencies
    run_gates     those plus lint / typecheck / build / test / e2e, compared with the base commit
    review_diff   a model that did not write the change reads it and reports findings

Nothing here writes to the checkout. Run it over stdio:

    asf-mcp --repo ~/code/myapp        (or: asf --repo ~/code/myapp mcp)

stdlib JSON-RPC over stdin/stdout, one line per message; the engine is ``asf/verify.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, TextIO

from .gates import COMMAND_GATES
from .verify import STATIC_GATES, VerifyError, Verifier

PROTOCOL = "2025-06-18"
MAX_LISTED_FILES = 40

_REPO = {"type": "string", "description": "repository to verify (default: the server's --repo)"}
_TIER = {"type": "integer", "description": "risk tier 0-3; higher means stricter gates (default 2)"}
_BASE = {"type": "string", "description": "commit or ref the change is measured against "
                                          "(default: merge base with the default branch)"}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_checks",
        "description": "What verification is available in this checkout and what the current change "
                       "touches: the base commit, the changed files, the gates that would run, the "
                       "packages affected and their commands. Runs no checks and costs nothing. "
                       "Call this first when you do not know how the repository is configured.",
        "inputSchema": {"type": "object",
                        "properties": {"repo": _REPO, "tier": _TIER, "base": _BASE}},
    },
    {
        "name": "check_diff",
        "description": "Fast read-only gates over the current change: secrets in added lines, diff "
                       "size against the tier limit, test integrity (tests weakened or deleted), and "
                       "new dependencies. Executes no project commands, so it answers in under a "
                       "second. Use it while you work; use run_gates before you claim to be done.",
        "inputSchema": {"type": "object",
                        "properties": {"repo": _REPO, "tier": _TIER, "base": _BASE}},
    },
    {
        "name": "run_gates",
        "description": "Run the full gate set over the current change and return a verdict: the "
                       "read-only gates plus the project's own lint, typecheck, build, test and e2e "
                       "commands, for the packages the diff touches. A failing command is re-run on "
                       "the base commit, so failures that were already there come back as "
                       "'preexisting' rather than being blamed on your change. This is the check to "
                       "run before reporting a change as finished; it can take minutes.",
        "inputSchema": {"type": "object", "properties": {
            "repo": _REPO, "tier": _TIER, "base": _BASE,
            "gates": {"type": "array", "items": {"type": "string"},
                      "description": f"subset to run; known gates are "
                                     f"{', '.join([*STATIC_GATES, *COMMAND_GATES])} "
                                     f"(default: every gate the tier requires)"},
            "compare_base": {"type": "boolean",
                             "description": "re-run failing commands on the base commit to separate "
                                            "pre-existing failures (default true)"}}},
    },
    {
        "name": "review_diff",
        "description": "Have a model that did not write the change read it cold and report findings "
                       "with severities (blocking, major, minor) and a verdict. It sees the diff and "
                       "the repository read-only and cannot edit anything. Costs model time, so run "
                       "the gates first and pass what you were trying to do as 'intent'.",
        "inputSchema": {"type": "object", "properties": {
            "repo": _REPO, "tier": _TIER, "base": _BASE,
            "intent": {"type": "string",
                       "description": "what the change is meant to do, in a sentence or two"},
            "role": {"type": "string", "enum": ["reviewer", "security"],
                     "description": "reviewer (default) judges correctness; security reads for "
                                    "vulnerabilities"}}},
    },
]


# ------------------------------------------------------------------------- output --

def _files(paths: list[str]) -> str:
    if not paths:
        return "no files changed against the base commit"
    shown = ", ".join(paths[:MAX_LISTED_FILES])
    extra = len(paths) - MAX_LISTED_FILES
    return f"{len(paths)} changed: {shown}" + (f", +{extra} more" if extra > 0 else "")


def fmt_checks(d: dict[str, Any]) -> str:
    lines = [f"{d['repo']} on {d['branch']}, base {d['base_sha']}",
             f"config: {d['config']}",
             _files(d["changed_files"]),
             f"gates at tier {d['tier']}: {', '.join(d['gates']) or 'none'}"]
    for p in d["packages_touched"]:
        cmds = ", ".join(f"{k}: {v}" for k, v in p["commands"].items()) or "no commands configured"
        lines.append(f"- package {p['package']} ({p['root']}), {p['changed_files']} files — {cmds}")
    if not d["packages_touched"]:
        lines.append("- no configured package owns the changed paths")
    if d["reviews"]:
        lines.append(f"reviews available: {', '.join(d['reviews'])} (review_diff)")
    return "\n".join(lines)


def fmt_gates(d: dict[str, Any]) -> str:
    head = (f"VERDICT: {d['verdict'].upper()} — {d['failed']} failing, {d['passed']} passing, "
            f"{d['preexisting']} pre-existing, {d['skipped']} skipped")
    lines = [head, f"base {d['base_sha']} on {d['branch']}; {_files(d['changed_files'])}"]
    if not d["compared_with_base"]:
        lines.append("(no base comparison: failures below are not split from pre-existing ones)")
    for r in d["results"]:
        where = f"{r['gate']}/{r['package']}" if r["package"] else r["gate"]
        lines.append(f"- {where}: {r['status'].upper()} — {r['detail']}")
        if r.get("log"):
            lines.append("  " + r["log"].rstrip().replace("\n", "\n  "))
    if d.get("unknown_gates"):
        lines.append(f"unknown gates ignored: {', '.join(d['unknown_gates'])}")
    return "\n".join(lines)


def fmt_review(d: dict[str, Any]) -> str:
    lines = [f"{d['role']} verdict: {d['verdict']} — {d['summary']}".rstrip(" —")]
    for f in d.get("findings", []):
        place = f"{f.get('file', '?')}:{f.get('line', '')}".rstrip(":")
        lines.append(f"- [{f.get('severity', 'minor')}] {place} {f.get('issue', '')}")
    if not d.get("findings"):
        lines.append("- no findings")
    if not d.get("ok", True):
        lines.append("(the reviewer did not finish; treat the verdict as incomplete)")
    lines.append(f"base {d['base_sha']}, {d.get('turns', 0)} turns, "
                 f"${d.get('cost_usd', 0):.2f}, model {d.get('model') or 'unknown'}")
    return "\n".join(lines)


# ------------------------------------------------------------------------- server --

class Server:
    """Holds one Verifier per repository; tool arguments may point at another one."""

    def __init__(self, repo: str | Path = ".", config: str | None = None,
                 state: str | None = None):
        self.default_repo, self.config, self.state = str(repo), config, state
        self._cache: dict[str, Verifier] = {}

    def verifier(self, repo: str | None) -> Verifier:
        key = str(Path(repo or self.default_repo).expanduser().resolve())
        if key not in self._cache:
            self._cache[key] = Verifier.open(key, self.config, self.state)
        return self._cache[key]

    def call(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Return (text, is_error). A blocked verdict is an answer, not an error."""
        args = dict(args or {})
        try:
            v = self.verifier(args.pop("repo", None))
            tier = int(args.get("tier", 2))
            base = args.get("base")
            if name == "list_checks":
                return fmt_checks(v.checks(tier=tier, base=base)), False
            if name == "check_diff":
                return fmt_gates(v.static(tier=tier, base=base)), False
            if name == "run_gates":
                return fmt_gates(v.gates(names=args.get("gates"), tier=tier, base=base,
                                         compare_base=bool(args.get("compare_base", True)))), False
            if name == "review_diff":
                return fmt_review(v.review(intent=args.get("intent", ""), tier=tier, base=base,
                                           role=args.get("role", "reviewer"))), False
            return f"no tool named {name}", True
        except VerifyError as e:
            return f"Error: {e}", True
        except Exception as e:  # noqa: BLE001 - a crashed check must not kill the server
            return f"Error: {type(e).__name__}: {e}", True

    # ------------------------------------------------------------------- stdio --
    def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {"protocolVersion": params.get("protocolVersion", PROTOCOL),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "asf-verify", "version": "1"}}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            text, is_error = self.call(params.get("name", ""), params.get("arguments") or {})
            return {"content": [{"type": "text", "text": text}], "isError": is_error}
        if method == "ping":
            return {}
        raise LookupError(method)

    def serve(self, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
        stdin, stdout = stdin or sys.stdin, stdout or sys.stdout

        def send(msg: dict[str, Any]) -> None:
            stdout.write(json.dumps(msg) + "\n")
            stdout.flush()

        for line in stdin:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            mid = msg.get("id")
            if mid is None:  # a notification: nothing to answer
                continue
            try:
                send({"jsonrpc": "2.0", "id": mid, "result": self.handle(msg.get("method", ""),
                                                                        msg.get("params") or {})})
            except LookupError as e:
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32601, "message": f"no method {e}"}})
            except Exception as e:  # noqa: BLE001
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}})
        return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="asf-mcp", description=__doc__.splitlines()[0])
    p.add_argument("--repo", default=".", help="repository to verify (default: current directory)")
    p.add_argument("--config", help="path to factory.yaml (default: the repo's, else detected)")
    p.add_argument("--state", help="state directory (default: $ASF_HOME/<repo>)")
    args = p.parse_args(argv)
    return Server(args.repo, args.config, args.state).serve()


if __name__ == "__main__":
    sys.exit(main())
