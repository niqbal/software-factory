"""Stand-in for the ``claude`` CLI in tests. No network, no model.

It parses the flags the factory passes, talks to the factory's MCP server over
HTTP exactly as Claude Code would (initialize, tools/list, tools/call), runs a
scripted list of tool calls, and prints a result object shaped like
``claude -p --output-format json``.

Environment:
  FAKE_CLAUDE_SCRIPT  JSON file: {"<role>": [[call, ...], ...], "structured": {...}}
                      Each inner list is one invocation; a resumed session takes the next.
  FAKE_CLAUDE_LOG     JSONL file: one record per invocation (argv, cwd, env, tool results).
  FAKE_CLAUDE_MODE    "not_logged_in" to answer like a CLI that is not signed in.
"""

import json
import os
import sys
import urllib.request
import uuid
from pathlib import Path


def opt(argv, name, default=None):
    if name in argv:
        i = argv.index(name)
        return argv[i + 1] if i + 1 < len(argv) else default
    return default


def rpc(url, token, method, params=None, notify=False):
    msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if not notify:
        msg["id"] = str(uuid.uuid4())
    req = urllib.request.Request(url, data=json.dumps(msg).encode(), method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
        "Authorization": token})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
        return json.loads(body) if body else None


def result(subtype="success", text="done", turns=1, is_error=False, session="", structured=None, model=""):
    out = {"type": "result", "subtype": subtype, "is_error": is_error, "num_turns": turns,
           "result": text, "session_id": session or str(uuid.uuid4()), "total_cost_usd": 0.01,
           "usage": {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 300,
                     "cache_creation_input_tokens": 40},
           "modelUsage": {model: {}} if model else {}}
    if structured is not None:
        out["structured_output"] = structured
    print("some startup noise on stdout")
    print(json.dumps(out))


def main():
    argv = sys.argv[1:]
    prompt = sys.stdin.read()
    model = opt(argv, "--model", "")
    log = {"argv": argv, "cwd": os.getcwd(), "prompt": prompt,
           "env_api_key": "ANTHROPIC_API_KEY" in os.environ, "results": []}
    system = Path(opt(argv, "--system-prompt-file")).read_text() if opt(argv, "--system-prompt-file") else ""
    role = system.split("[role:", 1)[1].split("]", 1)[0] if "[role:" in system else "unknown"
    log["role"] = role
    try:
        if os.environ.get("FAKE_CLAUDE_MODE") == "not_logged_in":
            return result(text="Not logged in · Please run /login", is_error=True)
        script = json.loads(Path(os.environ["FAKE_CLAUDE_SCRIPT"]).read_text())
        if "--json-schema" in argv:
            return result(structured=script.get("structured", {}).get(role, {}), model=model)

        server = json.loads(Path(opt(argv, "--mcp-config")).read_text())["mcpServers"]["asf"]
        url, token = server["url"], server["headers"]["Authorization"]
        init = rpc(url, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                               "clientInfo": {"name": "fake-claude", "version": "0"}})
        log["protocol"] = init["result"]["protocolVersion"]
        rpc(url, token, "notifications/initialized", notify=True)
        log["tools"] = [t["name"] for t in rpc(url, token, "tools/list")["result"]["tools"]]

        counter = Path(os.environ["FAKE_CLAUDE_SCRIPT"] + f".{role}.n")
        n = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(n + 1))
        invocations = script.get(role, [])
        calls = invocations[n] if n < len(invocations) else []
        allowed = (opt(argv, "--allowedTools") or "").split(",")
        for call in calls:
            name = call["name"]
            if f"mcp__asf__{name}" not in allowed:  # what --permission-mode dontAsk would do
                log["results"].append({"tool": name, "error": True, "text": "not allowed"})
                continue
            res = rpc(url, token, "tools/call", {"name": name, "arguments": call.get("input", {})})["result"]
            log["results"].append({"tool": name, "error": res["isError"], "text": res["content"][0]["text"]})
        session = opt(argv, "--resume") or f"session-{role}-{uuid.uuid4().hex[:8]}"
        max_turns = int(opt(argv, "--max-turns", "50"))
        if len(calls) >= max_turns:
            return result("error_max_turns", turns=max_turns, session=session, model=model)
        return result(turns=len(calls) + 1, session=session, model=model)
    finally:
        if os.environ.get("FAKE_CLAUDE_LOG"):
            with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
                f.write(json.dumps(log) + "\n")


if __name__ == "__main__":
    main()
