from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from agent_relay.claude_mcp import ClaudeMCPConfig, ClaudeMCPError, run_claude_mcp_task
from agent_relay.task import DelegationTask


def task() -> DelegationTask:
    return DelegationTask(
        task_id="mcp-transport-task",
        objective="Inspect the remote workspace and report one bounded result.",
        allowed_files=("value.py",),
        requirements=("Return a concise report.",),
        verification=("python -c \"assert True\"",),
        task_kind="documentation",
    )


class FakeMCPHandler(BaseHTTPRequestHandler):
    calls: list[dict] = []
    fail = False
    sse = False

    def log_message(self, *_args):
        return

    def do_POST(self):  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        self.__class__.calls.append({"method": body.get("method"), "body": body})
        if body.get("method") == "initialize":
            payload = {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {
                    "protocolVersion": "2025-03-26",
                    "serverInfo": {"name": "fake-claude-mcp", "version": "test"},
                    "capabilities": {"tools": {"listChanged": False}},
                },
            }
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/event-stream" if self.__class__.sse else "application/json",
            )
            self.send_header("MCP-Session-Id", "fake-session")
        elif body.get("method") == "notifications/initialized":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        else:
            payload = {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {
                    "content": [{"type": "text", "text": "exitCode: 0\nremote fixture completed"}],
                    "isError": self.__class__.fail,
                },
            }
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/event-stream" if self.__class__.sse else "application/json",
            )
        encoded_payload = json.dumps(payload)
        encoded = (
            f"event: message\ndata: {encoded_payload}\n\n".encode("utf-8")
            if self.__class__.sse
            else encoded_payload.encode("utf-8")
        )
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_DELETE(self):  # noqa: N802 - stdlib handler API
        self.__class__.calls.append({"method": "DELETE", "body": {}})
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def fake_mcp_server():
    FakeMCPHandler.calls = []
    FakeMCPHandler.fail = False
    FakeMCPHandler.sse = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMCPHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_claude_mcp_transport_initializes_calls_run_and_marks_remote_authority(fake_mcp_server) -> None:
    config = ClaudeMCPConfig(
        endpoint=f"http://127.0.0.1:{fake_mcp_server.server_port}/mcp",
        workdir="/remote/workspace",
        model="fixture-model",
        timeout_seconds=5,
    )
    result = run_claude_mcp_task(task(), config=config)

    assert result.status.value == "SUCCESS"
    assert result.summary.startswith("exitCode: 0")
    assert result.metadata["transport"] == "streamable-http-mcp"
    assert result.metadata["main_worktree_unchanged"] is None
    assert [item["method"] for item in FakeMCPHandler.calls] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
        "DELETE",
    ]
    call = FakeMCPHandler.calls[2]["body"]
    arguments = call["params"]["arguments"]
    assert arguments["workdir"] == "/remote/workspace"
    assert arguments["model"] == "fixture-model"
    assert "Inspect the remote workspace" in arguments["prompt"]
    assert "value.py" in arguments["prompt"]


def test_claude_mcp_transport_rejects_insecure_non_loopback_without_opt_in() -> None:
    with pytest.raises(ClaudeMCPError, match="non-loopback"):
        run_claude_mcp_task(
            task(),
            config=ClaudeMCPConfig(endpoint="http://10.0.0.207:8000/mcp"),
        )


def test_claude_mcp_transport_preserves_remote_tool_failure(fake_mcp_server) -> None:
    FakeMCPHandler.fail = True
    config = ClaudeMCPConfig(endpoint=f"http://127.0.0.1:{fake_mcp_server.server_port}/mcp", timeout_seconds=5)
    result = run_claude_mcp_task(task(), config=config)

    assert result.status.value == "WORKER_ERROR"
    assert "remote fixture completed" in result.blockers[0]


def test_claude_mcp_transport_decodes_streamable_http_sse(fake_mcp_server) -> None:
    FakeMCPHandler.sse = True
    config = ClaudeMCPConfig(
        endpoint=f"http://127.0.0.1:{fake_mcp_server.server_port}/mcp",
        timeout_seconds=5,
    )

    result = run_claude_mcp_task(task(), config=config)

    assert result.status.value == "SUCCESS"
    assert "remote fixture completed" in result.summary
