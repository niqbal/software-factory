import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from asf.adapters.base import ToolSpec
from asf.adapters.models.ollama import OllamaError, OllamaProvider


@pytest.fixture
def server():
    replies, seen = [], []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self._send({"models": []})

        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self._send({"message": {"role": "assistant", "content": replies.pop(0)},
                        "prompt_eval_count": 10, "eval_count": 3})

        def _send(self, d):
            body = json.dumps(d).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", replies, seen
    srv.shutdown()


TOOLS = [ToolSpec("read_file", "Read a file", {"type": "object"}), ToolSpec("finish", "Done", {"type": "object"})]


def test_turns_keep_history_constrain_format_and_retry_bad_json(server):
    url, replies, seen = server
    p = OllamaProvider({"base_url": url, "num_ctx": 8192})
    assert p.credentials_problem() is None
    replies += [json.dumps({"text": "look", "tool_calls": [{"name": "read_file", "input": {"path": "a"}}]}),
                "not json", json.dumps({"text": "done", "tool_calls": [{"name": "finish", "input": {}}]})]
    conv = p.conversation("gemma", "SYSTEM", TOOLS)
    conv.add_user("fix it")
    t1 = conv.step()
    assert [c.name for c in t1.tool_calls] == ["read_file"] and t1.cost_usd == 0 and t1.usage["input"] == 10
    conv.add_tool_results([(t1.tool_calls[0].id, "contents", False)])
    t2 = conv.step()
    assert t2.tool_calls[0].name == "finish" and t2.tool_calls[0].id != t1.tool_calls[0].id
    first, second, third = seen
    assert first["format"]["required"] == ["text", "tool_calls"] and first["options"]["num_ctx"] == 8192
    assert first["think"] is True  # medium effort thinks; low effort would not
    assert first["messages"][0]["role"] == "system" and "read_file" in first["messages"][0]["content"]
    assert "contents" in second["messages"][-1]["content"]          # tool results go back as a user turn
    assert "not a JSON object" in third["messages"][-1]["content"]  # a bad reply is asked again


def test_unreachable_server_is_a_credentials_problem():
    assert "ollama serve" in OllamaProvider({"base_url": "http://127.0.0.1:9"}).credentials_problem()


def test_gives_up_after_retries(server):
    url, replies, _ = server
    replies += ["x", "y", "z"]
    conv = OllamaProvider({"base_url": url, "retries": 2}).conversation("m", "S", TOOLS)
    conv.add_user("go")
    with pytest.raises(OllamaError):
        conv.step()
