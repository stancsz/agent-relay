from __future__ import annotations

from http.client import HTTPConnection
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
import time
from urllib.request import Request, urlopen

import pytest

from agent_relay.codex_proxy import OllamaCompatProxy


class _UpstreamState:
    def __init__(self) -> None:
        self.requests: list[tuple[str, bytes]] = []
        self.stream = False
        self.first_chunk_sent = Event()
        self.release_stream = Event()


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: _UpstreamState

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.state.requests.append((self.path, b""))
        body = b'{"models":[]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.state.requests.append((self.path, body))
        if self.state.stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"first")
            self.wfile.flush()
            self.state.first_chunk_sent.set()
            self.state.release_stream.wait(timeout=3)
            try:
                self.wfile.write(b"second")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()


@pytest.fixture
def upstream() -> tuple[_UpstreamState, ThreadingHTTPServer]:
    state = _UpstreamState()

    class Handler(_UpstreamHandler):
        pass

    Handler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, server
    finally:
        state.release_stream.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _upstream_url(server: ThreadingHTTPServer) -> str:
    host, port = server.server_address
    return f"http://{host}:{port}"


def test_proxy_rewrites_chat_requests_and_preserves_stats(
    upstream: tuple[_UpstreamState, ThreadingHTTPServer],
) -> None:
    state, server = upstream
    proxy = OllamaCompatProxy(
        _upstream_url(server),
        num_ctx=8192,
        num_predict=2048,
    ).start()
    try:
        payload = {
            "model": "qwen3.5:4b",
            "messages": [
                {"role": "system", "content": "large provider system"},
                {"role": "user", "content": "OK"},
            ],
            "reasoning_effort": "low",
            "tools": [{"type": "function", "function": {"name": "edit"}}],
        }
        request = Request(
            proxy.base_url + "/v1/chat/completions?stream=false",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            assert response.status == 200
            assert response.read() == b'{"ok":true}'

        with urlopen(proxy.base_url + "/api/tags", timeout=3) as response:
            assert response.read() == b'{"models":[]}'

        path, request_body = state.requests[0]
        forwarded = json.loads(request_body)
        assert path == "/v1/chat/completions?stream=false"
        assert forwarded["think"] is False
        assert "reasoning_effort" not in forwarded
        assert "tools" not in forwarded
        assert forwarded["messages"][0]["content"].startswith(
            "You are a bounded local coding worker"
        )
        assert forwarded["messages"][1]["content"].startswith("OK")
        assert "READY with patch=\"\" and files={} is invalid" in (
            forwarded["messages"][1]["content"]
        )
        assert forwarded["options"] == {"num_ctx": 8192, "num_predict": 2048}
        assert forwarded["max_tokens"] == 2048
        assert state.requests[1][0] == "/api/tags"
        assert proxy.stats["rewritten_chat_requests"] == 1
        assert proxy.stats["requests"] == 2
        assert proxy.stats["bytes_in"] > 0
        assert proxy.stats["bytes_forwarded"] > 0
        assert proxy.stats["bytes_out"] > 0
    finally:
        proxy.stop()

    # Stats remain available to the worker after the temporary server is shut
    # down and can therefore be included in the final runtime packet.
    assert proxy.stats["requests"] == 2


def test_proxy_rewrites_responses_requests_for_current_codex(
    upstream: tuple[_UpstreamState, ThreadingHTTPServer],
) -> None:
    state, server = upstream
    proxy = OllamaCompatProxy(
        _upstream_url(server),
        num_ctx=8192,
        num_predict=2048,
        temperature=0.0,
        seed=17,
    ).start()
    try:
        payload = {
            "model": "qwen3.5:4b",
            "instructions": "Keep the task bounded.",
            "input": [{
                "role": "user",
                "content": [{"type": "input_text", "text": "Return JSON."}],
            }],
            "reasoning": {"effort": "low"},
            "tools": [{"type": "function", "name": "edit"}],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "max_output_tokens": 4096,
        }
        request = Request(
            proxy.base_url + "/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            assert response.status == 200
            assert response.read() == b'{"ok":true}'

        forwarded = json.loads(state.requests[0][1])
        assert state.requests[0][0] == "/v1/responses"
        assert forwarded["reasoning"] == {"effort": "none"}
        assert "tools" not in forwarded
        assert "tool_choice" not in forwarded
        assert "parallel_tool_calls" not in forwarded
        assert "LOCAL COMPATIBILITY OVERRIDE" in forwarded["instructions"]
        assert forwarded["input"][0]["content"][0]["text"] == "Return JSON."
        assert forwarded["options"] == {
            "num_ctx": 8192,
            "num_predict": 2048,
            "temperature": 0.0,
            "seed": 17,
        }
        assert forwarded["max_output_tokens"] == 2048
        assert forwarded["temperature"] == 0.0
        assert forwarded["seed"] == 17
        assert proxy.stats["rewritten_responses_requests"] == 1
        assert proxy.stats["rewritten_chat_requests"] == 0
    finally:
        proxy.stop()


def test_proxy_forwards_response_bytes_incrementally(
    upstream: tuple[_UpstreamState, ThreadingHTTPServer],
) -> None:
    state, server = upstream
    state.stream = True
    proxy = OllamaCompatProxy(_upstream_url(server)).start()
    try:
        started = time.perf_counter()
        request = Request(
            proxy.base_url + "/v1/chat/completions",
            data=b'{"model":"qwen3.5:4b"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        response = urlopen(request, timeout=4)
        assert time.perf_counter() - started < 1.5
        assert state.first_chunk_sent.wait(timeout=1)
        assert response.read(5) == b"first"
        state.release_stream.set()
        assert response.read() == b"second"
        response.close()
    finally:
        state.release_stream.set()
        proxy.stop()


def test_proxy_stop_closes_active_upstream_without_waiting_for_model(
    upstream: tuple[_UpstreamState, ThreadingHTTPServer],
) -> None:
    state, server = upstream
    state.stream = True
    proxy = OllamaCompatProxy(_upstream_url(server), request_timeout=30).start()
    request = Request(
        proxy.base_url + "/v1/chat/completions",
        data=b'{"model":"qwen3.5:4b"}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    response_holder: list[object] = []

    def read_response() -> None:
        try:
            response = urlopen(request, timeout=5)
            response_holder.append(response)
            response.read()
        except OSError:
            return

    reader = Thread(target=read_response, daemon=True)
    reader.start()
    assert state.first_chunk_sent.wait(timeout=2)
    started = time.perf_counter()
    state.release_stream.set()
    proxy.stop()
    assert time.perf_counter() - started < 2
    reader.join(timeout=2)
    for response in response_holder:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def test_proxy_rejects_chunked_request_without_desynchronizing_connection(
    upstream: tuple[_UpstreamState, ThreadingHTTPServer],
) -> None:
    _state, server = upstream
    proxy = OllamaCompatProxy(_upstream_url(server)).start()
    host, port = proxy.base_url.removeprefix("http://").split(":")
    connection = HTTPConnection(host, int(port), timeout=3)
    try:
        connection.putrequest("POST", "/v1/chat/completions")
        connection.putheader("Transfer-Encoding", "chunked")
        connection.endheaders()
        connection.send(b"5\r\nhello\r\n0\r\n\r\n")
        response = connection.getresponse()
        assert response.status == 501
        assert json.loads(response.read()) == {
            "error": "chunked_request_not_supported"
        }
    finally:
        connection.close()
        proxy.stop()


def test_proxy_rejects_credentials_and_query_in_target() -> None:
    with pytest.raises(ValueError, match="credentials"):
        OllamaCompatProxy("http://user:pass@127.0.0.1:11434").start()
    with pytest.raises(ValueError, match="query"):
        OllamaCompatProxy("http://127.0.0.1:11434?token=secret").start()


def test_proxy_forwards_deterministic_sampling_controls(
    upstream: tuple[_UpstreamState, ThreadingHTTPServer],
) -> None:
    state, server = upstream
    proxy = OllamaCompatProxy(
        _upstream_url(server),
        temperature=0.0,
        seed=17,
    ).start()
    try:
        request = Request(
            proxy.base_url + "/v1/chat/completions",
            data=b'{"model":"qwen3.5:4b","messages":[]}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            assert response.status == 200
        forwarded = json.loads(state.requests[0][1])
        assert forwarded["temperature"] == 0.0
        assert forwarded["seed"] == 17
        assert forwarded["options"]["temperature"] == 0.0
        assert forwarded["options"]["seed"] == 17
    finally:
        proxy.stop()
