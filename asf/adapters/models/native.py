"""Native agent sessions: a coding agent CLI does the work with its own tools; the broker decides.

The emulated providers switch an agent CLI's tools off and have it request tool calls as JSON,
which costs most of what makes the agent good. A native provider instead runs the writing roles
(implementer, tester, CI fixer) as ordinary sessions of the CLI in the job's worktree: its own
system prompt, file tools, shell, subagents and project instruction files, in one continuous
context. What stays with asf:

- **Policy.** Every tool call goes through the CLI's pre-tool hook to this process
  (asf/native_bridge.py -> NativePolicy), which applies the broker's own path, secret,
  protected-file, test-integrity and command rules. Each decision is a `tool.call` event.
- **The factory's tools.** run_check and finish come from a small MCP server backed by the
  same broker, so checks run as the gates will and results land in the same fields.
- **An audit.** A shell can write files the hook never sees, so after each session every changed
  file is checked against the role's rules and anything not allowed is put back.

Each CLI (claude_native.py, cursor_native.py) supplies its tool names, how to start a session
and how to read its stream.
"""

from __future__ import annotations

import json
import re
import secrets
import shlex
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from ...broker import DENY_WORDS, Denied, ToolBroker
from ...models import AgentResult
from ..base import ModelTurn

BRIDGE = str(Path(__file__).resolve().parents[2] / "native_bridge.py")
AGENT_DIRS = (".claude", ".cursor")  # agent CLIs' project settings: an agent may not rewrite its own rules
FINISHERS = {"reviewer": "submit_review", "security": "submit_review"}


@dataclass
class ToolNames:
    """How one CLI names its tools, and which argument holds the path or command."""
    reads: dict[str, str]
    writes: dict[str, str]
    searches: dict[str, tuple[str, ...]]
    shells: dict[str, tuple[str, str]]          # tool -> (command key, working-folder key or "")
    mcp_prefix: str
    deletes: dict[str, str] = field(default_factory=dict)
    free: set[str] = field(default_factory=set)  # no file or shell access of their own


@dataclass
class Outcome:
    """The end of one session, read from the CLI's final message."""
    session: str | None
    turn: ModelTurn
    turns: int
    completed: bool        # ended normally (not by an error or one of its own limits)
    error: str = ""        # an API or usage-limit error: the job fails so the scheduler can act


class NativePolicy:
    """Decides a CLI's tool calls with the broker's rules."""

    def __init__(self, broker: ToolBroker, names: ToolNames, hidden: list[str] | None = None):
        self.b, self.names = broker, names
        self.root = broker.sb.root.resolve()
        self.hidden = list(hidden or [])  # files asf writes into the checkout for the session
        self.lock = threading.Lock()

    def _rel(self, path: str, transcript_dir: str = "") -> str | None:
        """Repository-relative path, or None for the CLI's own session files (always readable)."""
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.root / p
        p = p.resolve()
        if transcript_dir and str(p).startswith(transcript_dir.rstrip("/") + "/"):
            return None
        if p != self.root and not str(p).startswith(str(self.root) + "/"):
            raise Denied(f"{path} is outside the job's checkout")
        return self.b._rel(str(p.relative_to(self.root)) if p != self.root else ".")

    def decide(self, tool: str, args: dict[str, Any], transcript_path: str = "") -> tuple[str, str]:
        if tool.startswith(self.names.mcp_prefix):
            return "allow", ""  # the broker checks and logs these itself when they run
        transcript_dir = str(Path(transcript_path).parent) if transcript_path else ""
        with self.lock:
            self.b.state.calls += 1
            try:
                self._check(tool, args or {}, transcript_dir)
            except Denied as e:
                self.b._deny(tool, args, str(e))
                return "deny", str(e)
            self.b._log(tool, args, "allow", "")
            return "allow", ""

    def _check_read(self, rel: str) -> None:
        if rel in self.hidden:
            raise Denied(f"{rel} is the factory's session configuration")
        self.b._check_read(rel)

    def _check(self, tool: str, args: dict[str, Any], transcript_dir: str) -> None:
        n = self.names
        if tool in n.free:
            return
        if tool in n.reads:
            rel = self._rel(str(args.get(n.reads[tool]) or ""), transcript_dir)
            if rel is not None:
                self._check_read(rel)
        elif tool in n.searches:
            for key in n.searches[tool]:
                if args.get(key):
                    rel = self._rel(str(args[key]))
                    if rel not in (None, "."):
                        self._check_read(rel)
        elif tool in n.writes or tool in n.deletes:
            key = n.writes.get(tool) or n.deletes[tool]
            rel = self._rel(str(args.get(key) or ""))
            if rel is None or rel == ".":
                raise Denied("not a file in the checkout")
            deleting = tool in n.deletes
            self.check_write(rel, deleting=deleting)
            existed = (self.root / rel).exists()
            self.b.state.touched.add(rel)
            if not existed and not deleting:
                self.b.state.created.add(rel)
        elif tool in n.shells:
            command_key, cwd_key = n.shells[tool]
            if cwd_key and args.get(cwd_key):
                self._rel(str(args[cwd_key]))
            self.check_command(str(args.get(command_key) or ""))
        else:
            raise Denied(f"tool '{tool}' is not available to the {self.b.role} role")

    def check_write(self, rel: str, deleting: bool = False) -> None:
        for d in AGENT_DIRS:
            if rel == d or rel.startswith(d + "/"):
                raise Denied(f"{rel}: agent settings in {d}/ are protected")
        self.b._check_write(rel, deleting=deleting)

    def check_command(self, command: str) -> None:
        if DENY_WORDS.search(command) or any(p.search(command) for p in self.b.extra_denied):
            raise Denied(f"command not permitted: {command[:200]}")
        try:
            words = shlex.split(command, comments=True)
        except ValueError:
            words = command.split()
        from ...util import any_match
        for w in words:
            for part in re.split(r"[;&|<>()]+", w):
                part = part.removeprefix("./")
                if not part:
                    continue
                if part in self.hidden or any_match(part, self.b.secret_files) \
                        or any_match(Path(part).name, self.b.secret_files):
                    raise Denied(f"{part} is a secret or the factory's session configuration")

    def audit(self, before: dict[str, str | None]) -> list[str]:
        """Check files the session changed outside the file tools; revert the ones not allowed.

        ``before`` maps every file that differed from the base before the session to its content
        (None when it did not exist), so a reverted file returns to that state, not to the base.
        Only path rules apply: build output left by checks must not count toward the CI fixer's
        file limit (the emulated run_check never counted it either)."""
        reverted: list[str] = []
        limit, self.b.max_touch = self.b.max_touch, 10**9
        try:
            for rel in self.b.sb.changed_files():
                path = self.root / rel
                now = path.read_text(errors="replace") if path.is_file() else None
                if rel in before and before[rel] == now:
                    continue
                try:
                    self.check_write(rel, deleting=now is None)
                except Denied as e:
                    old = before[rel] if rel in before else self.b.sb.file_at_base(rel)
                    if old is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(old)
                    self.b._deny("shell", {"changed": rel}, f"changed by a shell command and reverted: {e}")
                    reverted.append(rel)
        finally:
            self.b.max_touch = limit
        return reverted


class Bridge:
    """The loopback endpoint the hook and the MCP server call, protected by a per-session token.

    ``check_wait`` bounds how long one run_check call blocks, for CLIs that give up on slow MCP
    calls: a longer check keeps running and the agent waits for it with check_result."""

    def __init__(self, broker: ToolBroker, policy: NativePolicy, check_wait: float | None = None):
        self.token = secrets.token_hex(16)
        self.broker, self.wait = broker, check_wait
        self.checks: dict[str, tuple[threading.Thread, dict[str, Any]]] = {}
        tools = [s for s in broker.tools() if s.name in ("run_check", "finish", "submit_review")]
        self.listing = [{"name": s.name, "description": s.description, "inputSchema": s.schema} for s in tools]
        if check_wait is not None and any(s.name == "run_check" for s in tools):
            self.listing.append({
                "name": "check_result",
                "description": "Wait for a run_check that reported it is still running, and get its output.",
                "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}})
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                if self.headers.get("X-ASF-Token") != bridge.token:
                    self.send_response(403)
                    self.end_headers()
                    return
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/hook":
                    decision, reason = policy.decide(body.get("tool_name", ""), body.get("tool_input") or {},
                                                     body.get("transcript_path") or "")
                    out: dict[str, Any] = {"decision": decision, "reason": reason}
                elif self.path == "/tools":
                    out = {"tools": bridge.listing}
                elif self.path == "/call":
                    text, err = bridge.call(body.get("name", ""), body.get("arguments") or {})
                    out = {"text": text, "is_error": err}
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def call(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        if self.wait is None:
            return self.broker.call(name, args)
        if name == "run_check":
            cid = f"C{len(self.checks) + 1}-{secrets.token_hex(2)}"
            holder: dict[str, Any] = {}
            t = threading.Thread(target=lambda: holder.update(out=self.broker.call(name, args)), daemon=True)
            t.start()
            self.checks[cid] = (t, holder)
            return self._await(cid)
        if name == "check_result":
            return self._await(str(args.get("id", "")))
        return self.broker.call(name, args)

    def _await(self, cid: str) -> tuple[str, bool]:
        if cid not in self.checks:
            return f"No running check with id {cid}.", True
        t, holder = self.checks[cid]
        t.join(self.wait)
        if t.is_alive():
            return (f"Still running (check {cid}). Call check_result with id {cid} to keep waiting "
                    "for its output.", False)
        del self.checks[cid]
        return holder["out"]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class NativeSessions:
    """The session loop shared by native providers. Subclasses describe their CLI."""

    names: ToolNames
    error_cls: type[Exception] = RuntimeError
    cli_name: str = ""  # in error messages: asf.limits tells backends apart by it
    check_wait: float | None = None
    binary: str | None
    native_roles: list[str]
    session_timeout: int

    # -- what each CLI provides ------------------------------------------------------------
    def _launch(self, sb: Any, broker: ToolBroker, bridge: Bridge, tmp: Path) -> Callable[[], None]:
        """Write the session's hook and MCP configuration; return what undoes it."""
        raise NotImplementedError

    def _command(self, model: str, effort: str, system: str, prompt: str, tmp: Path, max_turns: int | None,
                 resume: str | None, max_usd: float | None) -> tuple[list[str], str]:
        """Arguments and stdin for one session."""
        raise NotImplementedError

    def _env(self, sb: Any, bridge: Bridge) -> dict[str, str]:
        raise NotImplementedError

    def _tools_in(self, msg: dict[str, Any]) -> list[str]:
        """Tool calls a stream message starts."""
        raise NotImplementedError

    def _outcome(self, msg: dict[str, Any], model: str, tools: int) -> Outcome:
        raise NotImplementedError

    def _mcp_name(self, tool: str) -> str:
        """How the model sees one of asf's MCP tools, for the nudge."""
        return tool

    def hidden_files(self) -> list[str]:
        """Files the session's configuration puts in the checkout (unreadable to the agent)."""
        return []

    # -- shared ----------------------------------------------------------------------------
    def native_for(self, role: str, sandbox: Any) -> bool:
        # A real shell runs on this machine, so only sandboxes whose checkout is the boundary qualify.
        return role in self.native_roles and getattr(sandbox, "host_shell", False)

    def _session(self, args: list[str], stdin: str, cwd: Path, env: dict[str, str], timeout: float,
                 on_tool: Callable[[str], None]) -> tuple[dict[str, Any] | None, str]:
        """Run one session; return its final message (None if it never ended) and stderr."""
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, cwd=str(cwd), env=env)
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
        err: list[str] = []
        reader = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)  # type: ignore[union-attr]
        reader.start()
        result = None
        try:
            proc.stdin.write(stdin)  # type: ignore[union-attr]
            proc.stdin.close()  # type: ignore[union-attr]
            for line in proc.stdout:  # type: ignore[union-attr]
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "result":
                    result = msg
                else:
                    for name in self._tools_in(msg):
                        on_tool(name)
            proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        reader.join(timeout=5)
        timed_out = result is None and proc.returncode in (-9, 137)
        return result, ("timed out" if timed_out else "") + "".join(err)[-1000:]

    def run_native(self, role: str, model: str, effort: str, system: str, task: str, broker: ToolBroker,
                   charge: Callable[[str, ModelTurn], None], max_turns: int | None = None,
                   resume: str | None = None, budget_left: tuple[float | None, float | None] = (None, None),
                   ) -> AgentResult:
        if not self.binary:
            raise self.error_cls(self.credentials_problem() or "no binary")  # type: ignore[attr-defined]
        sb = broker.sb
        policy = NativePolicy(broker, self.names, self.hidden_files())
        before = {rel: ((sb.root / rel).read_text(errors="replace") if (sb.root / rel).is_file() else None)
                  for rel in sb.changed_files()}
        bridge = Bridge(broker, policy, self.check_wait)
        tmp = Path(tempfile.mkdtemp(prefix="asf-native-"))
        usd_left, minutes_left = budget_left
        timeout = self.session_timeout if minutes_left is None else max(60.0, min(self.session_timeout, minutes_left * 60))
        result = AgentResult(role=role, ok=False, summary="")
        tools_used: dict[str, int] = {}
        session, nudged, stop, reverted = resume, False, "", []
        undo: Callable[[], None] = lambda: None

        def on_tool(name: str) -> None:
            tools_used[name] = tools_used.get(name, 0) + 1

        prompt = task
        try:
            undo = self._launch(sb, broker, bridge, tmp)
            env = self._env(sb, bridge)
            for _ in range(2):
                calls_before = sum(tools_used.values())
                args, stdin = self._command(model, effort, system, prompt, tmp, max_turns, session,
                                            None if usd_left is None else usd_left - result.cost_usd)
                started = time.time()
                msg, err = self._session(args, stdin, sb.root, env, timeout, on_tool)
                timeout = max(60.0, timeout - (time.time() - started))
                if msg is None:
                    if not err.startswith("timed out"):
                        raise self.error_cls(f"{self.cli_name} ended without a result: {err.strip()[-300:]}")
                    stop = "timed out"
                    break
                out = self._outcome(msg, model, sum(tools_used.values()) - calls_before)
                session = out.session or session
                result.turns += out.turns
                result.input_tokens += out.turn.usage.get("input", 0) + out.turn.usage.get("cache_read", 0)
                result.output_tokens += out.turn.usage.get("output", 0)
                result.cost_usd += out.turn.cost_usd
                stop = out.turn.stop_reason
                charge(role, out.turn)
                if out.error and not broker.state.finished:
                    # API errors and usage limits fail like the other providers, so the scheduler
                    # can wait for the reset or move the job to another backend.
                    raise self.error_cls(out.error)
                if broker.state.finished or not out.completed or nudged:
                    break
                finisher = self._mcp_name(FINISHERS.get(role, "finish"))
                prompt = (f"You ended without calling {finisher}. If the task is not done, keep working. "
                          f"Call {finisher} only when it is done, or when a human must decide something "
                          "you cannot.")
                nudged = True
        finally:
            bridge.close()
            undo()
            reverted = policy.audit(before)
            for f in tmp.iterdir():
                f.unlink()
            tmp.rmdir()
        result.stop = stop
        result.data = dict(broker.state.result)
        result.data.update({"files_touched": sorted(broker.state.touched), "denials": broker.state.denials,
                            "finished": broker.state.finished, "nudged": nudged, "native": True,
                            "session_id": session, "tools_used": tools_used, "reverted": reverted})
        result.summary = result.data.get("summary", "")
        result.ok = broker.state.finished and not result.data.get("blocked_reason")
        if not broker.state.finished:
            result.summary = result.summary or f"{role} stopped ({stop or 'unknown'}) after {result.turns} turns without finishing"
        return result


def tool_notes(read: str, write: str, shell: str, run_check: str, finish: str, extra: str = "") -> str:
    """Maps the role prompt's generic tool names to the CLI's own."""
    return f"""

# How this session works
You are working in the job's own checkout (the current directory) with your own tools. The
instructions above name tools from the factory's generic protocol; here they map as follows:
- list_files, read_file, search, show_diff -> {read}, and `git diff` / `git status` in the shell
- write_file, edit_file, delete_file -> {write}
- run_command -> {shell}. It is a real shell in the checkout. Network access, installing packages
  and git commands that change history or remotes are refused; the factory commits and delivers.
- run_check -> {run_check}. Prefer it for the configured test, lint, typecheck, build and e2e
  commands: it runs them exactly as the factory's gates will (e2e starts the app for you).{extra}
- finish -> {finish}. Calling it is the only way your work is recorded. Call it once, at the end,
  with the summary and the other fields described above.
The factory's policy checks every tool call. A refusal is final: adapt instead of retrying it.
Files a shell command changes are checked the same way after you finish, and changes your role
may not make are reverted.
"""
