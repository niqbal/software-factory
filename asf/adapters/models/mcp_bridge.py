"""Expose the tool broker to an external agent runtime as an MCP server.

The factory's guarantee is that the broker is the only path from a model to the
sandbox. A runtime that brings its own agent loop, such as Claude Code, keeps
that guarantee only if its built-in tools are switched off and the broker's
tools are the only ones it can call. This module serves those tools over MCP's
streamable HTTP transport, in-process, so each call lands in ``broker.call``
with the same checks, redaction and audit log as the factory's own loop.

The server binds to 127.0.0.1 on a free port and requires a random bearer token
that only the spawned runtime receives, so other local processes cannot drive
the sandbox through it. It implements the small part of MCP a tool server
needs: initialize, ping, tools/list and tools/call, answered as plain JSON.
"""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from ..base import ToolSpec

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "asf"
MAX_BODY = 8 * 1024 * 1024

CallTool = Callable[[str, dict[str, Any]], tuple[str, bool]]


class BrokerMCPServer:
    """A loopback MCP server whose tools are the given specs, executed by ``call_tool``."""

    def __init__(self, tools: list[ToolSpec], call_tool: CallTool, name: str = SERVER_NAME):
        self.tools = tools
        self.call_tool = call_tool
        self.name = name
        self.token = secrets.token_urlsafe(32)
        self.calls: list[str] = []
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -------------------------------------------------------------- lifecycle --
    def start(self) -> "BrokerMCPServer":
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="asf-mcp", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def __enter__(self) -> "BrokerMCPServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    @property
    def url(self) -> str:
        assert self._httpd, "server not started"
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/mcp"

    def mcp_config(self) -> dict[str, Any]:
        """The ``--mcp-config`` document a Claude Code process needs to reach this server."""
        return {"mcpServers": {self.name: {"type": "http", "url": self.url,
                                           "headers": {"Authorization": f"Bearer {self.token}"}}}}

    def qualified_names(self) -> list[str]:
        """Tool names as Claude Code exposes them: ``mcp__<server>__<tool>``."""
        return [f"mcp__{self.name}__{t.name}" for t in self.tools]

    # --------------------------------------------------------------- protocol --
    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """Answer one JSON-RPC message. Notifications and responses get no reply."""
        if not isinstance(msg, dict) or "method" not in msg:
            return None
        if "id" not in msg:
            return None
        mid, method, params = msg["id"], msg["method"], msg.get("params") or {}
        try:
            result = self._dispatch(method, params)
        except _RpcError as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": e.code, "message": e.message}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            asked = params.get("protocolVersion")
            return {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": self.name, "version": "1"}}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [{"name": t.name, "description": t.description, "inputSchema": t.schema}
                              for t in self.tools]}
        if method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                raise _RpcError(-32602, "arguments must be an object")
            # The runtime may issue tool calls in parallel; the broker's state is not
            # thread-safe and its audit log should read in call order.
            with self._lock:
                self.calls.append(name)
                text, is_error = self.call_tool(name, args)
            return {"content": [{"type": "text", "text": text or "(empty)"}], "isError": bool(is_error)}
        raise _RpcError(-32601, f"method not found: {method}")


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _handler(server: BrokerMCPServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # keep the factory's output clean
            pass

        def _send(self, code: int, body: Any = None) -> None:
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(code)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _authorized(self) -> bool:
            got = self.headers.get("Authorization", "")
            return secrets.compare_digest(got.encode(), f"Bearer {server.token}".encode())

        def do_POST(self) -> None:  # noqa: N802 - http.server naming
            if self.path.split("?", 1)[0] != "/mcp":
                return self._send(404)
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                return self._send(400, {"error": "bad request body"})
            try:
                payload = json.loads(self.rfile.read(length))
            except ValueError:
                return self._send(400, {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32700, "message": "parse error"}})
            if isinstance(payload, list):
                replies = [r for r in (server.handle(m) for m in payload) if r is not None]
                return self._send(200, replies) if replies else self._send(202)
            reply = server.handle(payload)
            return self._send(200, reply) if reply is not None else self._send(202)

        def do_GET(self) -> None:  # noqa: N802 - no server-initiated stream
            self._send(405)

        def do_DELETE(self) -> None:  # noqa: N802 - sessions are not tracked
            self._send(405)

    return Handler
