"""Small MCP façade over the durable Agent Relay coordinator.

The coordinator remains the source of truth for leases, events, artifacts, and
receipts.  This module only translates a bounded MCP tool surface into the
existing authenticated HTTP API, so MCP clients do not need to know the REST
endpoints or database layout.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hmac
import json
import math
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from .agent_invocation import AgentInvocationConfig, AgentInvoker, AGENT_MODES, AGENT_NAMES
from .control import ControlPlaneError, request_json
from .protocol import JobState, TERMINAL_STATES
from .task import DelegationTask
from .worker_plane import WorkerConfig, run_worker_forever


MCP_PROTOCOL_VERSION = "2025-03-26"
MCP_MAX_BODY_BYTES = 8 * 1024 * 1024
MCP_MAX_TOOL_TEXT = 32_000
MCP_MAX_WATCH_SECONDS = 300.0
MCP_SERVER_INFO = {"name": "agent-relay-mcp", "version": "0.2.0"}


class MCPInputError(ValueError):
    """Raised when a tool call does not satisfy its bounded input contract."""


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _bounded_text(value: Any) -> str:
    return str(value)[:MCP_MAX_TOOL_TEXT]


def _rpc_ok(request_id: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": _bounded_text(message)}}


def _tool_result(value: Any, *, is_error: bool = False) -> dict[str, Any]:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if len(text) > MCP_MAX_TOOL_TEXT:
        text = text[:MCP_MAX_TOOL_TEXT] + "\n[tool text truncated]"
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": value,
        "isError": is_error,
    }


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MCPInputError(f"{label} must be an object")
    return value


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MCPInputError(f"{label} must be a non-empty string")
    return value.strip()


def _task_payload(value: Any) -> dict[str, Any]:
    raw = _require_object(value, "task")
    try:
        return DelegationTask.from_dict(raw).to_dict()
    except (TypeError, ValueError) as exc:
        raise MCPInputError(f"task is invalid: {exc}") from exc


_PROMPT_TASK_FIELDS = (
    "task_id",
    "allowed_files",
    "context",
    "requirements",
    "constraints",
    "verification",
    "success_criteria",
    "model",
    "retry_limit",
    "context_mode",
    "task_kind",
    "risk_flags",
)


def _task_from_arguments(args: Mapping[str, Any]) -> dict[str, Any]:
    """Accept either a durable task contract or Claude-MCP-style prompt input.

    Prompt mode is deliberately conservative: no ``allowed_files`` means a
    read-only task. Callers must opt into a write scope explicitly.
    """

    raw_task = args.get("task")
    prompt = args.get("prompt")
    if isinstance(raw_task, dict):
        if prompt is not None:
            raise MCPInputError("provide task or prompt, not both")
        return _task_payload(raw_task)
    if raw_task is not None and not isinstance(raw_task, str):
        raise MCPInputError("task must be an object or a prompt string")
    if raw_task is not None and prompt is not None:
        raise MCPInputError("provide task or prompt, not both")
    prompt_text = _require_text(prompt if prompt is not None else raw_task, "prompt")
    raw: dict[str, Any] = {
        "task_id": args.get("task_id", f"mcp-prompt-{secrets.token_hex(8)}"),
        "objective": prompt_text,
        "allowed_files": args.get("allowed_files", []),
    }
    for field in _PROMPT_TASK_FIELDS:
        if field in args and field != "task_id":
            raw[field] = args[field]
    if not raw["allowed_files"]:
        constraints = raw.get("constraints", [])
        if isinstance(constraints, str):
            constraints = [constraints]
        if isinstance(constraints, (list, tuple)):
            raw["constraints"] = list(constraints) + [
                "This prompt is read-only unless the caller explicitly declares allowed_files."
            ]
    return _task_payload(raw)


def _terminal(snapshot: Mapping[str, Any]) -> bool:
    state = snapshot.get("state")
    return isinstance(state, str) and state in {item.value for item in TERMINAL_STATES}


def _tool_specs(max_workers: int) -> list[dict[str, Any]]:
    task_property = {
        "type": "object",
        "description": "A complete bounded Agent Relay task contract.",
    }
    return [
        {
            "name": "agent_status",
            "description": "Describe local Gemini, Codex, and Claude CLI discovery without starting an agent or claiming authentication.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "invoke_agent",
            "description": "Invoke one installed Gemini, Codex, or Claude Code CLI with a bounded prompt. Read-only is the default; direct workspace writes require explicit server opt-in.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "enum": [*AGENT_NAMES],
                        "description": "Logical agent lane. Gemini auto-selects the usable direct Gemini or agy Gemini-backed transport.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Bounded instruction; no more than 24000 characters.",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Existing directory under the MCP agent workspace root; defaults to the root.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": [*AGENT_MODES],
                        "default": "read-only",
                    },
                    "model": {"type": "string", "description": "Optional backend-specific model override."},
                    "timeout_seconds": {
                        "type": "number",
                        "minimum": 1,
                        "description": "Per-call timeout, capped by the server configuration.",
                    },
                },
                "required": ["agent", "prompt"],
                "additionalProperties": False,
            },
        },
        {
            "name": "submit",
            "description": "Submit one durable bounded task or a safe read-only prompt to Agent Relay; execution is owned by an enrolled compatible worker.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": task_property,
                    "prompt": {"type": "string", "description": "Claude-MCP-style instruction; read-only unless allowed_files is declared."},
                    "task_id": {"type": "string"},
                    "allowed_files": {"type": "array", "items": {"type": "string"}},
                    "workdir": {"type": "string", "description": "Working directory; local workers require it under their repository, while claude-mcp passes it to the remote MCP server."},
                    "idempotency_key": {"type": "string"},
                    "requested_by": {"type": "string"},
                    "priority": {"type": "integer", "minimum": -1000, "maximum": 1000},
                    "deadline_at": {"type": "string", "description": "ISO-8601 timestamp with timezone."},
                },
                "oneOf": [{"required": ["task"]}, {"required": ["prompt"]}],
                "additionalProperties": True,
            },
        },
        {
            "name": "run",
            "description": "Claude-MCP-compatible run: accepts a task contract or prompt; with --local-worker it waits for a durable terminal receipt.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": task_property,
                    "prompt": {"type": "string", "description": "Natural-language instruction; read-only unless allowed_files is declared."},
                    "task_id": {"type": "string"},
                    "allowed_files": {"type": "array", "items": {"type": "string"}},
                    "workdir": {"type": "string", "description": "Working directory; local workers require it under their repository, while claude-mcp passes it to the remote MCP server."},
                    "wait": {"type": "boolean"},
                    "timeout_seconds": {"type": "number", "minimum": 0, "maximum": MCP_MAX_WATCH_SECONDS},
                    "interval_seconds": {"type": "number", "minimum": 0.05, "maximum": 30},
                },
                "oneOf": [{"required": ["task"]}, {"required": ["prompt"]}],
                "additionalProperties": True,
            },
        },
        {
            "name": "Agent",
            "description": "Claude-MCP-compatible Agent alias; accepts task or prompt and optionally waits for a durable receipt.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": task_property,
                    "prompt": {"type": "string", "description": "Natural-language instruction; read-only unless allowed_files is declared."},
                    "task_id": {"type": "string"},
                    "allowed_files": {"type": "array", "items": {"type": "string"}},
                    "workdir": {"type": "string", "description": "Working directory; local workers require it under their repository, while claude-mcp passes it to the remote MCP server."},
                    "wait": {"type": "boolean"},
                    "timeout_seconds": {"type": "number", "minimum": 0, "maximum": MCP_MAX_WATCH_SECONDS},
                    "interval_seconds": {"type": "number", "minimum": 0.05, "maximum": 30},
                },
                "oneOf": [{"required": ["task"]}, {"required": ["prompt"]}],
                "additionalProperties": True,
            },
        },
        {
            "name": "dispatch",
            "description": "Submit a bounded set of durable tasks with bounded submission concurrency; optionally wait for terminal snapshots.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workers": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": max_workers,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "task": task_property,
                                "prompt": {"type": "string"},
                                "allowed_files": {"type": "array", "items": {"type": "string"}},
                                "workdir": {"type": "string"},
                            },
                            "oneOf": [{"required": ["id", "task"]}, {"required": ["id", "prompt"]}],
                            "additionalProperties": True,
                        },
                    },
                    "max_concurrency": {"type": "integer", "minimum": 1, "maximum": max_workers},
                    "wait": {"type": "boolean", "description": "Poll each submitted task until terminal or timeout."},
                    "timeout_seconds": {"type": "number", "minimum": 0, "maximum": MCP_MAX_WATCH_SECONDS},
                },
                "required": ["workers"],
                "additionalProperties": False,
            },
        },
        {
            "name": "inspect",
            "description": "Read one durable task envelope, event history, and receipt snapshot.",
            "inputSchema": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "watch",
            "description": "Observe one durable task; optionally poll until a terminal state without resubmitting it.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "wait": {"type": "boolean"},
                    "timeout_seconds": {"type": "number", "minimum": 0, "maximum": MCP_MAX_WATCH_SECONDS},
                    "interval_seconds": {"type": "number", "minimum": 0.05, "maximum": 30},
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cancel",
            "description": "Request cancellation of one durable task; the receipt distinguishes a confirmed stop from an unproven stop.",
            "inputSchema": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}, "actor": {"type": "string"}},
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "chain_submit",
            "description": "Submit or defer one explicit predecessor-gated follow-up step with bounded parent inputs.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "chain_id": {"type": "string"},
                    "step_id": {"type": "string"},
                    "step_index": {"type": "integer", "minimum": 0},
                    "task": task_property,
                    "predecessor_task_id": {"type": "string"},
                    "parent_artifact_ids": {"type": "array", "items": {"type": "string"}},
                    "parent_messages": {"type": "array", "items": {"type": "string"}},
                    "allowed_predecessor_states": {"type": "array", "items": {"type": "string"}},
                    "defer_until_ready": {"type": "boolean"},
                    "priority": {"type": "integer", "minimum": -1000, "maximum": 1000},
                    "deadline_at": {"type": "string"},
                },
                "required": ["chain_id", "step_id", "step_index", "task"],
                "additionalProperties": False,
            },
        },
    ]


class RelayMCPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        coordinator_url: str,
        coordinator_token: str | None,
        auth_token: str | None,
        max_workers: int = 8,
        request_timeout: float = 30.0,
        local_worker: WorkerConfig | None = None,
        agent_invoker: AgentInvoker | None = None,
    ) -> None:
        self.coordinator_url = coordinator_url.rstrip("/")
        self.coordinator_token = coordinator_token
        self.auth_token = auth_token
        self.max_workers = max_workers
        self.request_timeout = request_timeout
        self.local_worker = local_worker
        self.agent_invoker = agent_invoker or AgentInvoker(AgentInvocationConfig.from_env())
        self.agent_slots = threading.BoundedSemaphore(self.agent_invoker.config.max_concurrency)
        self.sessions: dict[str, float] = {}
        self.sessions_lock = threading.Lock()
        super().__init__(address, RelayMCPRequestHandler)
        if local_worker is not None:
            worker_thread = threading.Thread(
                target=run_worker_forever,
                args=(local_worker,),
                name=f"agent-relay-mcp-worker-{local_worker.worker_id}",
                daemon=True,
            )
            worker_thread.start()

    def new_session(self) -> str:
        session_id = secrets.token_urlsafe(24)
        with self.sessions_lock:
            cutoff = time.time() - 3600
            self.sessions = {key: seen for key, seen in self.sessions.items() if seen >= cutoff}
            self.sessions[session_id] = time.time()
        return session_id

    def touch_session(self, session_id: str) -> bool:
        with self.sessions_lock:
            if session_id not in self.sessions:
                return False
            self.sessions[session_id] = time.time()
            return True

    def delete_session(self, session_id: str | None) -> None:
        if session_id:
            with self.sessions_lock:
                self.sessions.pop(session_id, None)

    def coordinator_request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return request_json(
            self.coordinator_url,
            method,
            path,
            payload=payload,
            auth_token=self.coordinator_token,
            timeout=self.request_timeout,
        )


class RelayMCPRequestHandler(BaseHTTPRequestHandler):
    server: RelayMCPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, payload: Mapping[str, Any], *, session_id: str | None = None) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if session_id:
            self.send_header("MCP-Session-Id", session_id)
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status: int, *, session_id: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        if session_id:
            self.send_header("MCP-Session-Id", session_id)
        self.end_headers()

    def _authorized(self) -> bool:
        if not self.server.auth_token:
            return True
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return bool(supplied) and hmac.compare_digest(supplied, self.server.auth_token)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise MCPInputError("Content-Length must be an integer") from exc
        if length <= 0 or length > MCP_MAX_BODY_BYTES:
            raise MCPInputError("MCP request body is empty or too large")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MCPInputError("MCP request body is not valid JSON") from exc
        return _require_object(value, "MCP request")

    def _session_for(self, request: Mapping[str, Any]) -> tuple[str | None, bool]:
        method = request.get("method")
        if method == "initialize":
            return None, True
        supplied = self.headers.get("MCP-Session-Id")
        if method == "ping" and not supplied:
            return None, True
        if not supplied or not self.server.touch_session(supplied):
            return supplied, False
        return supplied, True

    def _watch(self, task_id: str, *, wait: bool, timeout_seconds: float, interval_seconds: float) -> dict[str, Any]:
        started = time.monotonic()
        while True:
            snapshot = self.server.coordinator_request("GET", f"/tasks/{quote(task_id, safe='')}")
            if not wait or _terminal(snapshot):
                return {"task_id": task_id, "terminal": _terminal(snapshot), "timed_out": False, "snapshot": snapshot}
            if time.monotonic() - started >= timeout_seconds:
                return {"task_id": task_id, "terminal": False, "timed_out": True, "snapshot": snapshot}
            time.sleep(interval_seconds)

    def _workspace_policy(self, args: Mapping[str, Any]) -> dict[str, Any] | None:
        raw_policy = args.get("workspace_policy")
        if raw_policy is not None and not isinstance(raw_policy, dict):
            raise MCPInputError("workspace_policy must be an object")
        policy = dict(raw_policy or {})
        workdir = args.get("workdir")
        if workdir is None:
            return policy or None
        if not isinstance(workdir, str) or not workdir.strip():
            raise MCPInputError("workdir must be a non-empty string")
        if self.server.local_worker is None:
            raise MCPInputError("workdir requires MCP local-worker mode")
        if self.server.local_worker.backend == "claude-mcp":
            policy["mcp_workdir"] = workdir.strip()
            return policy
        root = self.server.local_worker.repo.expanduser().resolve()
        candidate = Path(workdir).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise MCPInputError("workdir must remain inside the configured local-worker repository") from exc
        if not candidate.is_dir():
            raise MCPInputError(f"workdir is not an existing directory: {candidate}")
        policy["workdir"] = str(candidate)
        return policy

    def _submission_payload(self, args: Mapping[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {"task": _task_from_arguments(args)}
        workspace_policy = self._workspace_policy(args)
        if workspace_policy is not None:
            payload["workspace_policy"] = workspace_policy
        for field in ("idempotency_key", "requested_by", "priority", "deadline_at", "correlation_id"):
            if field in args:
                payload[field] = args[field]
        return payload

    def _submit(self, args: Mapping[str, Any], *, wait_for_result: bool = False) -> dict[str, Any]:
        payload = self._submission_payload(args)
        submitted = self.server.coordinator_request("POST", "/tasks", payload)
        wait = bool(args.get("wait", wait_for_result))
        if not wait:
            return submitted
        timeout_seconds = min(max(float(args.get("timeout_seconds", 300.0)), 0.0), MCP_MAX_WATCH_SECONDS)
        interval_seconds = min(max(float(args.get("interval_seconds", 0.5)), 0.05), 30.0)
        return {
            "submission": submitted,
            "execution": self._watch(
                submitted["task_id"],
                wait=True,
                timeout_seconds=timeout_seconds,
                interval_seconds=interval_seconds,
            ),
            "execution_mode": "local-worker" if self.server.local_worker is not None else "coordinator-worker",
        }

    def _dispatch(self, args: Mapping[str, Any]) -> dict[str, Any]:
        raw_workers = args.get("workers")
        if not isinstance(raw_workers, list) or not raw_workers or len(raw_workers) > self.server.max_workers:
            raise MCPInputError(f"workers must contain 1-{self.server.max_workers} items")
        seen: set[str] = set()
        workers: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_workers):
            item = _require_object(raw, f"workers[{index}]")
            worker_id = _require_text(item.get("id"), f"workers[{index}].id")
            if worker_id in seen:
                raise MCPInputError(f"duplicate worker id: {worker_id}")
            seen.add(worker_id)
            if "task" not in item and "prompt" not in item:
                raise MCPInputError(f"workers[{index}] requires task or prompt")
            workers.append({"id": worker_id, "args": dict(item)})
        try:
            concurrency = int(args.get("max_concurrency", min(3, self.server.max_workers)))
        except (TypeError, ValueError) as exc:
            raise MCPInputError("max_concurrency must be an integer") from exc
        if not 1 <= concurrency <= self.server.max_workers:
            raise MCPInputError(f"max_concurrency must be between 1 and {self.server.max_workers}")
        wait = bool(args.get("wait", False))
        timeout_seconds = min(max(float(args.get("timeout_seconds", 60.0)), 0.0), MCP_MAX_WATCH_SECONDS)
        interval_seconds = min(max(float(args.get("interval_seconds", 0.5)), 0.05), 30.0)

        def run_one(item: dict[str, Any]) -> dict[str, Any]:
            try:
                submission_args = dict(item["args"])
                submission_args["idempotency_key"] = f"mcp-dispatch:{item['id']}"
                submitted = self.server.coordinator_request("POST", "/tasks", self._submission_payload(submission_args))
                result: dict[str, Any] = {"id": item["id"], "submission": submitted}
                if wait:
                    result["watch"] = self._watch(submitted["task_id"], wait=True, timeout_seconds=timeout_seconds, interval_seconds=interval_seconds)
                return result
            except ControlPlaneError as exc:
                return {"id": item["id"], "error": _bounded_text(str(exc))}

        with ThreadPoolExecutor(max_workers=min(concurrency, len(workers))) as pool:
            results = list(pool.map(run_one, workers))
        failed = sum(1 for item in results if "error" in item or item.get("watch", {}).get("snapshot", {}).get("state") in {state.value for state in (JobState.FAILED, JobState.BLOCKED, JobState.CANCELLED, JobState.EXPIRED)})
        return {"workers": results, "count": len(results), "failed": failed, "max_concurrency": min(concurrency, len(workers)), "waited": wait}

    def _chain_submit(self, args: Mapping[str, Any]) -> dict[str, Any]:
        chain_id = _require_text(args.get("chain_id"), "chain_id")
        step_id = _require_text(args.get("step_id"), "step_id")
        task = _task_payload(args.get("task"))
        try:
            step_index = int(args.get("step_index"))
        except (TypeError, ValueError) as exc:
            raise MCPInputError("step_index must be an integer") from exc
        payload: dict[str, Any] = {
            "step_id": step_id,
            "step_index": step_index,
            "task": task,
            "defer_until_ready": bool(args.get("defer_until_ready", False)),
            "parent_artifact_ids": args.get("parent_artifact_ids", []),
            "parent_messages": args.get("parent_messages", []),
            "allowed_predecessor_states": args.get("allowed_predecessor_states", [JobState.SUCCEEDED.value]),
        }
        for field in ("predecessor_task_id", "idempotency_key", "requested_by", "correlation_id", "workspace_policy", "priority", "deadline_at"):
            if field in args:
                payload[field] = args[field]
        return self.server.coordinator_request("POST", f"/chains/{quote(chain_id, safe='')}/steps", payload)

    def _invoke_agent(self, args: Mapping[str, Any]) -> dict[str, Any]:
        agent = _require_text(args.get("agent"), "agent")
        prompt = _require_text(args.get("prompt"), "prompt")
        mode = args.get("mode", "read-only")
        if not isinstance(mode, str):
            raise MCPInputError("mode must be a string")
        timeout_raw = args.get("timeout_seconds")
        if isinstance(timeout_raw, bool):
            raise MCPInputError("timeout_seconds must be a number")
        try:
            timeout_seconds = (
                self.server.agent_invoker.config.timeout_seconds
                if timeout_raw is None
                else float(timeout_raw)
            )
        except (TypeError, ValueError) as exc:
            raise MCPInputError("timeout_seconds must be a number") from exc
        if (
            not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > self.server.agent_invoker.config.timeout_seconds
        ):
            raise MCPInputError(
                "timeout_seconds must be positive and no greater than the configured agent timeout"
            )
        acquired = self.server.agent_slots.acquire(timeout=min(timeout_seconds, 5.0))
        if not acquired:
            return _tool_result(
                {
                    "agent": agent,
                    "status": "FAILED",
                    "summary": "agent concurrency limit reached",
                    "runtime": {"failure_kind": "agent_concurrency_limit"},
                },
                is_error=True,
            )
        try:
            try:
                result = self.server.agent_invoker.invoke(
                    agent,
                    prompt,
                    workdir=args.get("workdir"),
                    mode=mode,
                    model=args.get("model"),
                    timeout_seconds=timeout_seconds,
                )
            except (TypeError, ValueError) as exc:
                raise MCPInputError(str(exc)) from exc
            return _tool_result(result.to_dict(), is_error=not result.passed)
        finally:
            self.server.agent_slots.release()

    def _call_tool(self, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
        if name == "agent_status":
            if args:
                raise MCPInputError("agent_status does not accept arguments")
            return _tool_result(self.server.agent_invoker.status())
        if name == "invoke_agent":
            return self._invoke_agent(args)
        if name == "submit":
            return _tool_result(self._submit(args))
        if name in {"run", "Agent"}:
            return _tool_result(self._submit(args, wait_for_result=self.server.local_worker is not None))
        if name == "dispatch":
            return _tool_result(self._dispatch(args))
        if name == "inspect":
            task_id = _require_text(args.get("task_id"), "task_id")
            return _tool_result(self.server.coordinator_request("GET", f"/tasks/{quote(task_id, safe='')}"))
        if name == "watch":
            task_id = _require_text(args.get("task_id"), "task_id")
            wait = bool(args.get("wait", False))
            timeout_seconds = min(max(float(args.get("timeout_seconds", 60.0)), 0.0), MCP_MAX_WATCH_SECONDS)
            interval_seconds = min(max(float(args.get("interval_seconds", 0.5)), 0.05), 30.0)
            return _tool_result(self._watch(task_id, wait=wait, timeout_seconds=timeout_seconds, interval_seconds=interval_seconds))
        if name == "cancel":
            task_id = _require_text(args.get("task_id"), "task_id")
            actor = args.get("actor", "client")
            return _tool_result(self.server.coordinator_request("POST", f"/tasks/{quote(task_id, safe='')}/cancel", {"actor": actor}))
        if name == "chain_submit":
            return _tool_result(self._chain_submit(args))
        raise MCPInputError(f"unknown tool: {name}")

    def _handle_rpc(self, request: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None, bool]:
        request_id = request.get("id")
        method = request.get("method")
        if not isinstance(method, str):
            return _rpc_error(request_id, -32600, "method is required"), None, False
        session_id, valid = self._session_for(request)
        if not valid:
            return _rpc_error(request_id, -32600, "missing or unknown MCP session"), session_id, False
        if method == "initialize":
            session_id = self.server.new_session()
            return _rpc_ok(
                request_id,
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "serverInfo": MCP_SERVER_INFO,
                    "capabilities": {"tools": {"listChanged": False}},
                },
            ), session_id, False
        if method == "notifications/initialized":
            return None, session_id, True
        if method == "ping":
            return _rpc_ok(request_id, {}), session_id, False
        if method == "tools/list":
            return _rpc_ok(request_id, {"tools": _tool_specs(self.server.max_workers)}), session_id, False
        if method != "tools/call":
            return _rpc_error(request_id, -32601, f"method not found: {method}"), session_id, False
        params = _require_object(request.get("params", {}), "params")
        name = _require_text(params.get("name"), "tool name")
        args = _require_object(params.get("arguments", {}), "tool arguments")
        try:
            result = self._call_tool(name, args)
        except MCPInputError as exc:
            return _rpc_error(request_id, -32602, str(exc)), session_id, False
        except ControlPlaneError as exc:
            result = _tool_result({"status": "error", "error": _bounded_text(str(exc))}, is_error=True)
        except Exception as exc:  # keep the MCP process alive while bounding details
            result = _tool_result({"status": "error", "error": f"{type(exc).__name__}: {_bounded_text(exc)}"}, is_error=True)
        return _rpc_ok(request_id, result), session_id, False

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler API
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Accept, MCP-Session-Id, MCP-Protocol-Version")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "POST, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._authorized():
            self._send_empty(HTTPStatus.UNAUTHORIZED)
            return
        self.server.delete_session(self.headers.get("MCP-Session-Id"))
        self._send_empty(HTTPStatus.OK)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if not (self.path == "/mcp" or self.path.startswith("/mcp?")):
            self._send_empty(HTTPStatus.NOT_FOUND)
            return
        if not self._authorized():
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        try:
            request = self._read_json()
            response, session_id, empty = self._handle_rpc(request)
            if empty:
                self._send_empty(HTTPStatus.NO_CONTENT, session_id=session_id)
            elif response is not None:
                self._send_json(HTTPStatus.OK, response, session_id=session_id)
        except MCPInputError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, _rpc_error(None, -32700, str(exc)))
        except Exception as exc:  # bound protocol failures, never terminate the server
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, _rpc_error(None, -32603, f"MCP server error: {type(exc).__name__}"))


def create_mcp_server(
    *,
    host: str,
    port: int,
    coordinator_url: str,
    coordinator_token: str | None = None,
    auth_token: str | None = None,
    max_workers: int = 8,
    request_timeout: float = 30.0,
    local_worker: WorkerConfig | None = None,
    agent_invoker: AgentInvoker | None = None,
    agent_config: AgentInvocationConfig | None = None,
) -> RelayMCPServer:
    if host not in {"127.0.0.1", "localhost", "::1"} and not auth_token:
        raise ValueError("non-loopback MCP binds require an auth token")
    if not 1 <= max_workers <= 32:
        raise ValueError("max_workers must be between 1 and 32")
    return RelayMCPServer(
        (host, port),
        coordinator_url=coordinator_url,
        coordinator_token=coordinator_token,
        auth_token=auth_token,
        max_workers=max_workers,
        request_timeout=request_timeout,
        local_worker=local_worker,
        agent_invoker=agent_invoker or (AgentInvoker(agent_config) if agent_config else None),
    )


def serve_mcp_forever(
    *,
    host: str,
    port: int,
    coordinator_url: str,
    coordinator_token: str | None = None,
    auth_token: str | None = None,
    max_workers: int = 8,
    request_timeout: float = 30.0,
    local_worker: WorkerConfig | None = None,
    agent_invoker: AgentInvoker | None = None,
    agent_config: AgentInvocationConfig | None = None,
) -> None:
    server = create_mcp_server(
        host=host,
        port=port,
        coordinator_url=coordinator_url,
        coordinator_token=coordinator_token,
        auth_token=auth_token,
        max_workers=max_workers,
        request_timeout=request_timeout,
        local_worker=local_worker,
        agent_invoker=agent_invoker,
        agent_config=agent_config,
    )
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
