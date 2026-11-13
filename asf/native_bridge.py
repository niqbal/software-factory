"""Processes Claude Code starts for a native factory session: the policy hook and the asf MCP server.

Both forward to the factory process that owns the job (ASF_BRIDGE_URL, ASF_BRIDGE_TOKEN), where
the tool broker decides and executes. Stdlib only: this runs once per tool call.

    python -I .../asf/native_bridge.py hook [claude|cursor]   # pre-tool hook: JSON in, decision out
    python -I .../asf/native_bridge.py mcp    # MCP stdio server exposing run_check and finish
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request


def post(path: str, body: dict, timeout: float = 30) -> dict:
    req = urllib.request.Request(os.environ["ASF_BRIDGE_URL"] + path, data=json.dumps(body).encode(),
                                 method="POST", headers={"Content-Type": "application/json",
                                                         "X-ASF-Token": os.environ["ASF_BRIDGE_TOKEN"]})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def hook(cli: str = "claude") -> int:
    event = json.load(sys.stdin)
    try:
        d = post("/hook", {k: event.get(k) for k in ("tool_name", "tool_input", "transcript_path")})
        decision, reason = d["decision"], d.get("reason", "")
    except Exception as e:  # noqa: BLE001 - no answer from the factory means no permission
        decision, reason = "deny", f"factory policy check unavailable: {e}"
    if cli == "cursor":
        print(json.dumps({"permission": decision, "agent_message": reason, "user_message": reason}))
    else:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                                 "permissionDecisionReason": reason}}))
    return 0


def mcp() -> int:
    timeout = float(os.environ.get("ASF_BRIDGE_TOOL_TIMEOUT", "3600"))
    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        mid, method, params = msg.get("id"), msg.get("method", ""), msg.get("params") or {}
        if mid is None:  # a notification
            continue
        try:
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                          "capabilities": {"tools": {}}, "serverInfo": {"name": "asf", "version": "1"}}
            elif method == "tools/list":
                result = {"tools": post("/tools", {})["tools"]}
            elif method == "tools/call":
                r = post("/call", {"name": params.get("name"), "arguments": params.get("arguments") or {}}, timeout)
                result = {"content": [{"type": "text", "text": r["text"]}], "isError": r["is_error"]}
            elif method == "ping":
                result = {}
            else:
                reply = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}}
                print(json.dumps(reply), flush=True)
                continue
            reply = {"jsonrpc": "2.0", "id": mid, "result": result}
        except Exception as e:  # noqa: BLE001
            reply = {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"Error: {e}"}],
                                                             "isError": True}}
        print(json.dumps(reply), flush=True)
    return 0


if __name__ == "__main__":
    if sys.argv[1] == "hook":
        sys.exit(hook(sys.argv[2] if len(sys.argv) > 2 else "claude"))
    sys.exit(mcp())
