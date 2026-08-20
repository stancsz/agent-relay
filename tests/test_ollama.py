from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from typing import ClassVar

from agent_relay.ollama import OllamaClient, OllamaConfig


class OllamaHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[tuple[str, dict, dict]]] = []

    def do_GET(self) -> None:
        self.requests.append((self.path, {}, dict(self.headers)))
        if self.path == "/api/tags":
            self._send({"models": [{"name": "test-model", "digest": "local"}]})
        elif self.path == "/api/version":
            self._send({"version": "test"})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        self.requests.append((self.path, body, dict(self.headers)))
        response = {
            "status": "READY",
            "summary": "test response",
            "patch": "diff --git a/value.py b/value.py\n--- a/value.py\n+++ b/value.py\n",
            "blockers": [],
        }
        self._send({"model": body["model"], "response": json.dumps(response)})

    def _send(self, value: dict) -> None:
        data = json.dumps(value).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return


def test_ollama_tags_and_generate_send_auth() -> None:
    OllamaHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), OllamaHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = OllamaConfig(
            host=f"http://127.0.0.1:{server.server_port}",
            default_model="test-model",
            api_key="secret",
            timeout_seconds=5,
            seed=17,
        )
        client = OllamaClient(config)
        assert client.list_models()[0]["name"] == "test-model"
        assert client.version() == "test"
        generation = client.generate("system", "prompt")
        assert generation.model == "test-model"
        assert '"status": "READY"' in generation.text
        assert any(
            path == "/api/generate"
            and {key.lower(): value for key, value in headers.items()}.get("x-api-key") == "secret"
            and {key.lower(): value for key, value in headers.items()}.get("authorization") == "Bearer secret"
            for path, _, headers in OllamaHandler.requests
        )
        post_bodies = [body for path, body, _ in OllamaHandler.requests if path == "/api/generate"]
        assert post_bodies[-1]["think"] is False
        assert post_bodies[-1]["options"]["seed"] == 17
        json_generation = client.generate("system", "prompt", json_mode=True, think=False)
        assert json_generation.model == "test-model"
        assert OllamaHandler.requests[-1][1]["format"] == "json"
        assert OllamaHandler.requests[-1][1]["think"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_ollama_config_accepts_thinking_levels(monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_THINK", "medium")
    assert OllamaConfig.from_env().think == "medium"


def test_ollama_config_defaults_to_qwen35_4b(monkeypatch) -> None:
    monkeypatch.delenv("LOCAL_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    assert OllamaConfig.from_env().default_model == "qwen3.5:4b"
