"""Loopback compatibility proxy for Codex CLI and Ollama-compatible APIs.

Some Ollama-compatible endpoints expose Qwen's reasoning in a separate
``reasoning`` field and leave ``message.content`` empty unless the request
explicitly disables thinking.  Codex CLI's older Chat Completions path waits
for usable assistant content and can otherwise appear stalled forever.

Current Codex CLI releases use the Responses API. Ollama supports that API,
but its Responses endpoint uses ``reasoning.effort`` rather than the Chat
Completions ``think`` flag. The proxy applies the equivalent bounded controls
to both paths so the current lane does not silently re-enable long Qwen
reasoning or tool schemas.

The proxy is intentionally narrow:

* it binds only to loopback;
* it forwards only to the configured Ollama host;
* it rewrites only Chat Completions or Responses JSON bodies;
* it exists only for the lifetime of one worker attempt.

The outer sandbox and verifier remain independent of this transport adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Lock, Thread
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


_MAX_REQUEST_BYTES = 16 * 1024 * 1024
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_COMPACT_SYSTEM_PROMPT = (
    "You are a bounded local coding worker. Follow the final user task exactly. "
    "Tools are intentionally unavailable in this compatibility lane, so you "
    "cannot edit the sandbox or run commands. Work only within the declared "
    "allowed files and make the smallest change. Return exactly one JSON object "
    "with status, summary, patch, files, and blockers. Prefer a files map with "
    "complete current content for one allowed file; use patch only for a multi-"
    "file task. Otherwise use a standard unified diff with diff --git, --- a/path, "
    "+++ b/path, and a valid hunk. Escape newlines inside JSON strings. Never "
    "include prose, Markdown fences, fake index lines, or a partial hunk. A READY "
    "result MUST have a non-empty complete unified patch or non-empty complete "
    "file contents; never return READY with both patch and files empty. Do not "
    "return prose."
)
_NO_TOOLS_USER_OVERRIDE = (
    "\n\nLOCAL COMPATIBILITY OVERRIDE: tools are unavailable. You must return a "
    "non-empty complete unified diff in patch, or complete current contents in "
    "files. For one allowed file, files must contain the complete current content "
    "and patch must be empty. READY with patch=\"\" and files={} is invalid."
)
_HOP_BY_HOP_HEADERS = frozenset({
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
})


def _root_host(value: str) -> str:
    """Normalize a configured Ollama root without accidentally duplicating /v1."""

    raw = value.strip().rstrip("/")
    if not raw:
        raise ValueError("Ollama host must not be empty")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Ollama host must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("Ollama host must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("Ollama host must not contain a query or fragment")
    if parsed.path.rstrip("/") == "/v1":
        parsed = parsed._replace(path="")
    return urlunsplit(parsed)


def _blocked_headers(headers: Mapping[str, str]) -> set[str]:
    blocked = set(_HOP_BY_HOP_HEADERS)
    connection = headers.get("Connection") or headers.get("connection")
    if connection:
        blocked.update(
            token.strip().casefold()
            for token in connection.split(",")
            if token.strip()
        )
    return blocked


def _append_responses_no_tools_override(payload: dict[str, Any]) -> None:
    """Tell a Responses-capable local model to return the bounded result.

    Responses requests carry the system portion in ``instructions`` and the
    user turn in ``input``. Preserve both verbatim and append the compact
    contract to the last usable text field; unlike the Chat lane, replacing
    the whole system message would discard Codex's execution instructions.
    """

    instructions = payload.get("instructions")
    if isinstance(instructions, str):
        if _NO_TOOLS_USER_OVERRIDE not in instructions:
            payload["instructions"] = instructions + _NO_TOOLS_USER_OVERRIDE
        return

    input_value = payload.get("input")
    if isinstance(input_value, str):
        payload["input"] = input_value + _NO_TOOLS_USER_OVERRIDE
        return
    if isinstance(input_value, list):
        for index in range(len(input_value) - 1, -1, -1):
            item = input_value[index]
            if not isinstance(item, dict) or item.get("role") != "user":
                continue
            updated = dict(item)
            content = updated.get("content")
            if isinstance(content, str):
                updated["content"] = content + _NO_TOOLS_USER_OVERRIDE
            elif isinstance(content, list):
                content_items = list(content)
                for content_index in range(len(content_items) - 1, -1, -1):
                    content_item = content_items[content_index]
                    if not isinstance(content_item, dict):
                        continue
                    text = content_item.get("text")
                    if isinstance(text, str):
                        content_item = dict(content_item)
                        content_item["text"] = text + _NO_TOOLS_USER_OVERRIDE
                        content_items[content_index] = content_item
                        break
                else:
                    content_items.append(
                        {"type": "input_text", "text": _NO_TOOLS_USER_OVERRIDE}
                    )
                updated["content"] = content_items
            else:
                updated["content"] = _NO_TOOLS_USER_OVERRIDE
            input_value[index] = updated
            payload["input"] = input_value
            return
        input_value.append(
            {"role": "user", "content": _NO_TOOLS_USER_OVERRIDE}
        )
        payload["input"] = input_value
        return

    payload["input"] = _NO_TOOLS_USER_OVERRIDE


@dataclass
class ProxyStats:
    requests: int = 0
    rewritten_chat_requests: int = 0
    rewritten_responses_requests: int = 0
    upstream_errors: int = 0
    bytes_in: int = 0
    bytes_forwarded: int = 0
    bytes_out: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def add_request(
        self,
        *,
        incoming: int,
        rewritten: bool,
        endpoint: str | None = None,
    ) -> None:
        with self._lock:
            self.requests += 1
            self.bytes_in += incoming
            if rewritten:
                if endpoint == "/v1/responses":
                    self.rewritten_responses_requests += 1
                else:
                    self.rewritten_chat_requests += 1

    def add_forwarded(self, *, outgoing: int) -> None:
        with self._lock:
            self.bytes_forwarded += outgoing

    def add_response(self, *, outgoing: int) -> None:
        with self._lock:
            self.bytes_out += outgoing

    def add_error(self) -> None:
        with self._lock:
            self.upstream_errors += 1

    def to_dict(self) -> dict[str, int]:
        with self._lock:
            return {
                "requests": self.requests,
                "rewritten_chat_requests": self.rewritten_chat_requests,
                "rewritten_responses_requests": self.rewritten_responses_requests,
                "upstream_errors": self.upstream_errors,
                "bytes_in": self.bytes_in,
                "bytes_forwarded": self.bytes_forwarded,
                "bytes_out": self.bytes_out,
            }


class _ProxyServer(ThreadingHTTPServer):
    allow_reuse_address = True
    # The worker owns the proxy lifetime. Cancellation must return even if a
    # client or provider socket is already wedged; active upstream responses
    # are closed explicitly in stop(), and any remaining handler is daemonized
    # so it cannot hold the parent worker open during retry/cleanup.
    daemon_threads = True
    block_on_close = False

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        target_host: str,
        disable_reasoning: bool,
        request_timeout: float,
        num_ctx: int | None,
        num_predict: int | None,
        temperature: float | None,
        seed: int | None,
        strip_tools: bool,
        compact_prompt: bool,
    ) -> None:
        super().__init__(address, handler)
        self.target_host = target_host
        self.disable_reasoning = disable_reasoning
        self.request_timeout = request_timeout
        self.num_ctx = num_ctx
        self.num_predict = num_predict
        self.temperature = temperature
        self.seed = seed
        self.strip_tools = strip_tools
        self.compact_prompt = compact_prompt
        self.stats = ProxyStats()
        self._active_lock = Lock()
        self._active_responses: set[Any] = set()

    def register_response(self, response: Any) -> None:
        with self._active_lock:
            self._active_responses.add(response)

    def unregister_response(self, response: Any) -> None:
        with self._active_lock:
            self._active_responses.discard(response)

    def close_active_responses(self) -> None:
        with self._active_lock:
            responses = tuple(self._active_responses)
        for response in responses:
            try:
                response.close()
            except OSError:
                pass

    def handle_error(self, _request: Any, _client_address: Any) -> None:
        # A client can disappear while an upstream streaming response is being
        # closed during worker cancellation. Keep prompts/provider details out
        # of stderr and retain only bounded accounting.
        self.stats.add_error()


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        # Do not copy model prompts or provider responses into stderr/logs.
        return

    @property
    def proxy_server(self) -> _ProxyServer:
        server = self.server
        if not isinstance(server, _ProxyServer):
            raise RuntimeError("unexpected proxy server type")
        return server

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._forward()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._forward()

    def _forward(self) -> None:
        if self.headers.get("Transfer-Encoding"):
            self._respond(501, b'{"error":"chunked_request_not_supported"}')
            return
        length_text = self.headers.get("Content-Length", "0")
        try:
            length = int(length_text)
        except ValueError:
            self._respond(400, b'{"error":"invalid content length"}')
            return
        if length < 0 or length > _MAX_REQUEST_BYTES:
            self._respond(413, b'{"error":"request body too large"}')
            return
        body = self.rfile.read(length) if length else None
        if length and body is not None and len(body) != length:
            self._respond(400, b'{"error":"incomplete request body"}')
            return
        rewritten = False
        endpoint = self.path.split("?", 1)[0].rstrip("/")
        if body and endpoint in {"/v1/chat/completions", "/v1/responses"}:
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                if endpoint == "/v1/responses":
                    if self.proxy_server.disable_reasoning:
                        # Ollama's Responses endpoint ignores Chat's ``think``
                        # flag. ``none`` is the provider-native equivalent.
                        payload["reasoning"] = {"effort": "none"}
                        payload.pop("reasoning_effort", None)
                    if self.proxy_server.strip_tools:
                        for key in ("tools", "tool_choice", "parallel_tool_calls"):
                            payload.pop(key, None)
                        if self.proxy_server.compact_prompt:
                            _append_responses_no_tools_override(payload)
                else:
                    if self.proxy_server.disable_reasoning:
                        payload["think"] = False
                        # This field is rejected by some Ollama-compatible
                        # gateways; the local Codex reasoning setting is not a
                        # provider request parameter, so remove it at this
                        # boundary.
                        payload.pop("reasoning_effort", None)
                    if self.proxy_server.strip_tools:
                        for key in (
                            "tools",
                            "tool_choice",
                            "parallel_tool_calls",
                        ):
                            payload.pop(key, None)
                        if self.proxy_server.compact_prompt:
                            messages = payload.get("messages")
                            if isinstance(messages, list):
                                user_messages = [
                                    message
                                    for message in messages
                                    if isinstance(message, dict)
                                    and message.get("role") == "user"
                                ]
                                if user_messages:
                                    last_user = dict(user_messages[-1])
                                    content = last_user.get("content")
                                    if isinstance(content, str):
                                        last_user["content"] = (
                                            content + _NO_TOOLS_USER_OVERRIDE
                                        )
                                    payload["messages"] = [
                                        {
                                            "role": "system",
                                            "content": _COMPACT_SYSTEM_PROMPT,
                                        },
                                        last_user,
                                    ]
                if (
                    self.proxy_server.num_ctx is not None
                    or self.proxy_server.num_predict is not None
                    or self.proxy_server.temperature is not None
                    or self.proxy_server.seed is not None
                ):
                    options = payload.get("options")
                    if not isinstance(options, dict):
                        options = {}
                        payload["options"] = options
                    if self.proxy_server.num_ctx is not None:
                        options["num_ctx"] = self.proxy_server.num_ctx
                    if self.proxy_server.num_predict is not None:
                        options["num_predict"] = self.proxy_server.num_predict
                        if endpoint == "/v1/chat/completions":
                            # Ollama's native API reads ``options.num_predict``,
                            # while its OpenAI-compatible Chat endpoint honors
                            # the OpenAI token-limit field.
                            for key in ("max_tokens", "max_completion_tokens"):
                                existing = payload.get(key)
                                if (
                                    isinstance(existing, int)
                                    and not isinstance(existing, bool)
                                    and existing > 0
                                ):
                                    payload[key] = min(
                                        existing,
                                        self.proxy_server.num_predict,
                                    )
                            if (
                                "max_tokens" not in payload
                                and "max_completion_tokens" not in payload
                            ):
                                payload["max_tokens"] = self.proxy_server.num_predict
                        else:
                            existing = payload.get("max_output_tokens")
                            if (
                                isinstance(existing, int)
                                and not isinstance(existing, bool)
                                and existing > 0
                            ):
                                payload["max_output_tokens"] = min(
                                    existing,
                                    self.proxy_server.num_predict,
                                )
                            elif "max_output_tokens" not in payload:
                                payload["max_output_tokens"] = self.proxy_server.num_predict
                    if self.proxy_server.temperature is not None:
                        payload["temperature"] = self.proxy_server.temperature
                        options["temperature"] = self.proxy_server.temperature
                    if self.proxy_server.seed is not None:
                        payload["seed"] = self.proxy_server.seed
                        options["seed"] = self.proxy_server.seed
                if (
                    self.proxy_server.disable_reasoning
                    or self.proxy_server.strip_tools
                    or self.proxy_server.compact_prompt
                    or self.proxy_server.num_ctx is not None
                    or self.proxy_server.num_predict is not None
                    or self.proxy_server.temperature is not None
                    or self.proxy_server.seed is not None
                ):
                    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    rewritten = True

        self.proxy_server.stats.add_request(
            incoming=length,
            rewritten=rewritten,
            endpoint=endpoint,
        )
        request_headers = {
            key: value
            for key, value in self.headers.items()
            if key.casefold() not in _blocked_headers(self.headers)
        }
        if body is not None:
            request_headers["Content-Length"] = str(len(body))
        self.proxy_server.stats.add_forwarded(outgoing=len(body or b""))
        response_started = False
        try:
            request = Request(
                f"{self.proxy_server.target_host}{self.path}",
                data=body,
                headers=request_headers,
                method=self.command,
            )
            with urlopen(
                request,
                timeout=self.proxy_server.request_timeout,
            ) as response:
                self.proxy_server.register_response(response)
                try:
                    status = response.status
                    response_headers = dict(response.headers.items())
                    content_length = _content_length(response_headers)
                    if content_length is not None and content_length > _MAX_RESPONSE_BYTES:
                        self.proxy_server.stats.add_error()
                        self._respond(502, b'{"error":"upstream_response_too_large"}')
                        return
                    self._begin_response(status, response_headers)
                    response_started = True
                    response_bytes = 0
                    while True:
                        response_body = response.read(64 * 1024)
                        if not response_body:
                            break
                        response_bytes += len(response_body)
                        if response_bytes > _MAX_RESPONSE_BYTES:
                            self.proxy_server.stats.add_error()
                            break
                        try:
                            self.wfile.write(response_body)
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            self.close_connection = True
                            return
                        self.proxy_server.stats.add_response(outgoing=len(response_body))
                    self.close_connection = True
                    return
                finally:
                    self.proxy_server.unregister_response(response)
        except HTTPError as exc:
            response_body = exc.read(_MAX_RESPONSE_BYTES + 1)
            if len(response_body) > _MAX_RESPONSE_BYTES:
                response_body = b'{"error":"upstream_error_body_too_large"}'
            status = exc.code
            response_headers = dict(exc.headers.items())
            self.proxy_server.stats.add_error()
        except (OSError, URLError, TimeoutError, AttributeError) as exc:
            if response_started:
                self.close_connection = True
                return
            response_body = b'{"error":"local_upstream_failure"}'
            status = 502
            response_headers = {"Content-Type": "application/json"}
            self.proxy_server.stats.add_error()
        self._respond(status, response_body, response_headers)

    def _begin_response(self, status: int, headers: dict[str, str]) -> None:
        """Start a response while preserving incremental upstream output."""

        self.send_response(status)
        content_length = None
        blocked = _blocked_headers(headers) | {"date", "server"}
        for key, value in headers.items():
            normalized = key.casefold()
            if normalized == "content-length":
                content_length = value
                continue
            if normalized in blocked:
                continue
            self.send_header(key, value)
        if content_length is not None:
            self.send_header("Content-Length", content_length)
        else:
            # An Ollama streaming response is often chunked upstream. The
            # proxy deliberately removes that hop-by-hop framing and uses a
            # close-delimited HTTP/1.1 response, allowing bytes to reach Codex
            # as soon as they arrive without buffering the model response.
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()

    def _respond(
        self,
        status: int,
        body: bytes,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        blocked = _blocked_headers(headers or {}) | {
            "date",
            "server",
            "content-length",
        }
        for key, value in (headers or {}).items():
            normalized = key.casefold()
            if normalized in blocked:
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True


def _content_length(headers: Mapping[str, str]) -> int | None:
    value = headers.get("Content-Length") or headers.get("content-length")
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None

@dataclass
class OllamaCompatProxy:
    """Serve a temporary loopback endpoint that normalizes Ollama requests."""

    target_host: str
    disable_reasoning: bool = True
    request_timeout: float = 300.0
    num_ctx: int | None = None
    num_predict: int | None = None
    temperature: float | None = None
    seed: int | None = None
    strip_tools: bool = True
    compact_prompt: bool = True
    _server: _ProxyServer | None = field(default=None, init=False, repr=False)
    _thread: Thread | None = field(default=None, init=False, repr=False)
    _stats_snapshot: dict[str, int] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def start(self) -> "OllamaCompatProxy":
        if self._server is not None:
            raise RuntimeError("Ollama compatibility proxy is already running")
        target = _root_host(self.target_host)
        if self.request_timeout <= 0:
            raise ValueError("proxy request_timeout must be positive")
        for name, value in (
            ("num_ctx", self.num_ctx),
            ("num_predict", self.num_predict),
            ("seed", self.seed),
        ):
            if value is not None and (not isinstance(value, int) or value <= 0):
                raise ValueError(f"proxy {name} must be a positive integer")
        if self.temperature is not None and (
            not isinstance(self.temperature, (int, float))
            or isinstance(self.temperature, bool)
            or self.temperature < 0
        ):
            raise ValueError("proxy temperature must be a nonnegative number")
        server = _ProxyServer(
            ("127.0.0.1", 0),
            _ProxyHandler,
            target_host=target,
            disable_reasoning=self.disable_reasoning,
            request_timeout=self.request_timeout,
            num_ctx=self.num_ctx,
            num_predict=self.num_predict,
            temperature=self.temperature,
            seed=self.seed,
            strip_tools=self.strip_tools,
            compact_prompt=self.compact_prompt,
        )
        self._stats_snapshot = {}
        thread = Thread(
            target=server.serve_forever,
            name="lcd-ollama-compat-proxy",
            daemon=True,
        )
        thread.start()
        self._server = server
        self._thread = thread
        return self

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is None:
            return
        self._stats_snapshot = server.stats.to_dict()
        server.close_active_responses()
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5)

    def __enter__(self) -> "OllamaCompatProxy":
        return self.start()

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.stop()

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("Ollama compatibility proxy is not running")
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    @property
    def stats(self) -> dict[str, int]:
        if self._server is None:
            return dict(self._stats_snapshot)
        return self._server.stats.to_dict()
