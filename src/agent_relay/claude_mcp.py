"""Bounded client for an existing Claude streamable-HTTP MCP server.

This transport is intentionally distinct from ``claude-task``.  A remote MCP
server owns the Claude process and filesystem, so Agent Relay records the
remote output and transport identity but never claims local patch, sandbox, or
verification authority that it did not observe.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import ssl
import threading
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .result import DelegationResult, ResultStatus
from .task import DelegationTask


class ClaudeMCPError(RuntimeError):
    """Raised when the remote Claude MCP transport cannot complete a call."""


@dataclass(frozen=True)
class ClaudeMCPConfig:
    endpoint: str = "http://127.0.0.1:8000/mcp"
    auth_token: str | None = None
    workdir: str = "."
    model: str | None = None
    timeout_seconds: float = 300.0
    allow_insecure_lan: bool = False

    @classmethod
    def from_env(cls) -> "ClaudeMCPConfig":
        try:
            timeout = float(os.environ.get("AR_CLAUDE_MCP_TIMEOUT_SECONDS", "300"))
        except ValueError:
            timeout = 300.0
        if timeout <= 0:
            raise ValueError("Claude MCP timeout must be greater than zero")
        return cls(
            endpoint=(
                os.environ.get("AR_CLAUDE_MCP_URL")
                or os.environ.get("CLAUDE_MCP_URL")
                or cls.endpoint
            ),
            auth_token=os.environ.get("AR_CLAUDE_MCP_AUTH_TOKEN")
            or os.environ.get("CLAUDE_MCP_AUTH_TOKEN"),
            workdir=os.environ.get("AR_CLAUDE_MCP_WORKDIR", "."),
            model=os.environ.get("AR_CLAUDE_MCP_MODEL"),
            timeout_seconds=timeout,
            allow_insecure_lan=os.environ.get("AR_CLAUDE_MCP_ALLOW_INSECURE_LAN", "").lower()
            in {"1", "true", "yes"},
        )


def _endpoint(config: ClaudeMCPConfig) -> tuple[str, str | None]:
    value = config.endpoint.rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ClaudeMCPError("Claude MCP URL must be an absolute http(s) URL")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        if not config.allow_insecure_lan:
            raise ClaudeMCPError(
                "non-loopback Claude MCP HTTP requires HTTPS or "
                "AR_CLAUDE_MCP_ALLOW_INSECURE_LAN=1"
            )
    return value, parsed.hostname


def _request_json(
    endpoint: str,
    method: str,
    payload: Mapping[str, Any] | None = None,
    *,
    timeout: float,
    auth_token: str | None,
    session_id: str | None = None,
) -> tuple[dict[str, Any] | None, Mapping[str, str]]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-03-26",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    if session_id:
        headers["MCP-Session-Id"] = session_id
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(endpoint, method=method, data=body, headers=headers)
    try:
        context = ssl.create_default_context() if endpoint.lower().startswith("https://") else None
        with urlopen(request, timeout=timeout, context=context) as response:
            raw = response.read()
            response_headers = dict(response.headers.items())
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ClaudeMCPError(f"Claude MCP HTTP {exc.code}: {detail[:500]}") from exc
    except (OSError, URLError) as exc:
        raise ClaudeMCPError(f"Claude MCP request {method} {endpoint} failed: {exc}") from exc
    if not raw:
        return None, response_headers
    try:
        value = _decode_mcp_response(raw, response_headers.get("Content-Type", ""))
    except UnicodeDecodeError as exc:
        raise ClaudeMCPError("Claude MCP returned a non-UTF-8 response") from exc
    return value, response_headers


def _session_id(headers: Mapping[str, str]) -> str | None:
    for key, value in headers.items():
        if key.lower() == "mcp-session-id" and value.strip():
            return value.strip()
    return None


def _decode_mcp_response(raw: bytes, content_type: str) -> dict[str, Any]:
    """Decode one JSON-RPC response from JSON or Streamable HTTP SSE."""

    text = raw.decode("utf-8")
    candidates = [text]
    if "text/event-stream" in content_type.lower() or not text.lstrip().startswith("{"):
        event_data: list[str] = []
        events: list[str] = []
        for line in text.splitlines():
            if not line.strip():
                if event_data:
                    events.append("\n".join(event_data))
                    event_data = []
                continue
            if line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if separator and field.strip() == "data":
                event_data.append(value[1:] if value.startswith(" ") else value)
        if event_data:
            events.append("\n".join(event_data))
        candidates = events + candidates

    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    raise ClaudeMCPError("Claude MCP returned neither JSON nor an SSE JSON-RPC response")


def _text_from_tool_result(result: Mapping[str, Any]) -> str:
    pieces: list[str] = []
    content = result.get("content", [])
    if isinstance(content, list):
        for item in content:
            if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                pieces.append(item["text"])
    structured = result.get("structuredContent")
    if not pieces and structured is not None:
        pieces.append(json.dumps(structured, ensure_ascii=False, indent=2))
    return "\n\n".join(pieces)[:20_000]


def _prompt(task: DelegationTask) -> str:
    lines = [
        "You are executing one bounded task through Agent Relay.",
        f"Objective: {task.objective}",
        "Write scope: " + (", ".join(task.allowed_files) if task.allowed_files else "none; do not modify files"),
    ]
    for label, values in (
        ("Requirements", task.requirements),
        ("Constraints", task.constraints),
        ("Success criteria", task.success_criteria),
        ("Verification commands", task.verification),
    ):
        if values:
            lines.append(f"{label}:\n" + "\n".join(f"- {item[:2_000]}" for item in values[:16]))
    lines.append(
        "Return a concise final report describing what you observed or changed, "
        "what verification ran, and any blockers. Do not commit, push, deploy, "
        "or claim verification that did not run."
    )
    return "\n\n".join(lines)[:30_000]


def run_claude_mcp_task(
    task: DelegationTask,
    *,
    config: ClaudeMCPConfig | None = None,
    cancel_event: threading.Event | None = None,
) -> DelegationResult:
    """Run a task through an existing Claude MCP ``run`` tool."""

    selected = config or ClaudeMCPConfig.from_env()
    endpoint, hostname = _endpoint(selected)
    if cancel_event is not None and cancel_event.is_set():
        return DelegationResult(
            task_id=task.task_id,
            status=ResultStatus.BLOCKED,
            summary="Claude MCP task was cancelled before dispatch.",
            blockers=("cancel requested before remote MCP dispatch",),
            metadata={"lane": "claude-mcp", "execution_stopped": True},
        )
    session_id: str | None = None
    try:
        initialize, headers = _request_json(
            endpoint,
            "POST",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "agent-relay", "version": "0.1.0"},
                },
            },
            timeout=min(30.0, selected.timeout_seconds),
            auth_token=selected.auth_token,
        )
        session_id = _session_id(headers)
        if isinstance(initialize, Mapping) and "error" in initialize:
            raise ClaudeMCPError(str(initialize["error"])[:500])
        if session_id:
            _request_json(
                endpoint,
                "POST",
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                timeout=min(30.0, selected.timeout_seconds),
                auth_token=selected.auth_token,
                session_id=session_id,
            )
        response, _ = _request_json(
            endpoint,
            "POST",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "run",
                    "arguments": {
                        "prompt": _prompt(task),
                        "workdir": selected.workdir,
                        **({"model": task.model or selected.model} if task.model or selected.model else {}),
                    },
                },
            },
            timeout=selected.timeout_seconds + 15.0,
            auth_token=selected.auth_token,
            session_id=session_id,
        )
        if not isinstance(response, Mapping):
            raise ClaudeMCPError("Claude MCP did not return a JSON-RPC response")
        if "error" in response:
            raise ClaudeMCPError(str(response["error"])[:500])
        result = response.get("result", {})
        if not isinstance(result, Mapping):
            raise ClaudeMCPError("Claude MCP result is malformed")
        output = _text_from_tool_result(result)
        failed = result.get("isError") is True
        cancelled = bool(cancel_event and cancel_event.is_set())
        status = ResultStatus.BLOCKED if cancelled else (ResultStatus.WORKER_ERROR if failed else ResultStatus.SUCCESS)
        server_info: Mapping[str, Any] = {}
        if isinstance(initialize, Mapping) and isinstance(initialize.get("result"), Mapping):
            candidate = initialize["result"].get("serverInfo")
            if isinstance(candidate, Mapping):
                server_info = dict(candidate)
        return DelegationResult(
            task_id=task.task_id,
            status=status,
            summary=output or "Claude MCP task completed without textual output.",
            blockers=(output[:1_000],) if failed or cancelled else (),
            attempts=1,
            sandbox_mode="remote-mcp",
            metadata={
                "lane": "claude-mcp",
                "transport": "streamable-http-mcp",
                "remote_endpoint": endpoint,
                "remote_host": hostname,
                "remote_workdir": selected.workdir,
                "verification_authority": "remote-mcp-output-only",
                "main_worktree_unchanged": None,
                "execution_stopped": False if cancelled else None,
                "server_info": server_info,
            },
        )
    except ClaudeMCPError as exc:
        return DelegationResult(
            task_id=task.task_id,
            status=ResultStatus.WORKER_ERROR,
            summary=f"Claude MCP task could not run: {exc}",
            blockers=(str(exc),),
            metadata={
                "lane": "claude-mcp",
                "transport": "streamable-http-mcp",
                "remote_endpoint": endpoint,
                "main_worktree_unchanged": None,
            },
        )
    finally:
        if session_id:
            try:
                _request_json(
                    endpoint,
                    "DELETE",
                    timeout=5.0,
                    auth_token=selected.auth_token,
                    session_id=session_id,
                )
            except ClaudeMCPError:
                pass
