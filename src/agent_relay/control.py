"""Local/LAN coordinator and HTTP client for the durable Agent Relay plane."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
from collections.abc import Iterator
import ssl
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from .protocol import (
    AgentCard,
    JobEnvelope,
    JobReceipt,
    JobState,
    ProtocolError,
    PROTOCOL_VERSION,
    Readiness,
    TERMINAL_STATES,
    utc_now,
)
from .store import (
    AgentAccessError,
    ChainConflict,
    ChainInputError,
    ChainNotReady,
    IdempotencyConflict,
    JobNotFound,
    LeaseConflict,
    LeaseNotFound,
    RelayStore,
    StoreError,
)
from .task import DelegationTask


MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_TEXT = 32_000
MAX_STREAM_SECONDS = 30.0


class ControlPlaneError(RuntimeError):
    """Raised for HTTP control-plane failures."""


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _bounded_text(value: Any) -> str:
    return str(value)[:MAX_RESPONSE_TEXT]


def _task_submission(body: Mapping[str, Any]) -> JobEnvelope:
    if "envelope" in body:
        return JobEnvelope.from_dict(body["envelope"])
    raw_task = body.get("task", body)
    task = DelegationTask.from_dict(raw_task)
    return JobEnvelope.new(
        task,
        idempotency_key=body.get("idempotency_key") or task.task_id,
        requested_by=body.get("requested_by", "client"),
        correlation_id=body.get("correlation_id"),
        workspace_policy=body.get("workspace_policy", {}),
        priority=body.get("priority", 0),
        deadline_at=body.get("deadline_at"),
    )


def _path_parts(path: str) -> list[str]:
    return [part for part in path.strip("/").split("/") if part]


class RelayHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], store: RelayStore, auth_token: str | None) -> None:
        self.store = store
        self.auth_token = auth_token
        self.tls_enabled = False
        self.server_started_at = utc_now()
        super().__init__(address, RelayRequestHandler)

    def enable_tls(self, certificate: str | Path, private_key: str | Path) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(
            certfile=str(Path(certificate).expanduser().resolve()),
            keyfile=str(Path(private_key).expanduser().resolve()),
        )
        self.socket = context.wrap_socket(self.socket, server_side=True)
        self.tls_enabled = True


class RelayRequestHandler(BaseHTTPRequestHandler):
    server: RelayHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Structured callers can inspect responses; avoid leaking task payloads
        # into an unbounded access log by default.
        return

    def _authorized(self, *, health: bool = False) -> bool:
        token = self.server.auth_token
        if not token:
            return True
        if health:
            return True
        header = self.headers.get("Authorization", "")
        supplied = header.removeprefix("Bearer ").strip()
        return bool(supplied) and hmac.compare_digest(supplied, token)

    def _send(self, status: int, payload: Mapping[str, Any]) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, *, error_type: str = "error") -> None:
        self._send(status, {"protocol": PROTOCOL_VERSION, "status": "error", "error": error_type, "message": _bounded_text(message)})

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "0")
        except ValueError as exc:
            raise ProtocolError("Content-Length must be an integer") from exc
        if length <= 0:
            raise ProtocolError("request body must not be empty")
        if length > MAX_REQUEST_BYTES:
            raise ProtocolError(f"request body exceeds {MAX_REQUEST_BYTES} bytes")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("request body must be UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ProtocolError("request body must be a JSON object")
        return value

    def _require_auth(self, *, health: bool = False) -> bool:
        if self._authorized(health=health):
            return True
        self._error(HTTPStatus.UNAUTHORIZED, "Bearer authentication required", error_type="unauthorized")
        return False

    def _require_request_auth(self, parts: list[str], body: Mapping[str, Any]) -> bool:
        """Authorize admin requests or a worker-scoped mutation.

        The coordinator bearer remains the admin/client credential. Workers
        may use an enrolled credential only when the claimed actor matches the
        credential's Agent Card identity.
        """

        if self._authorized():
            return True
        actor: Any = None
        if parts == ["agents", "register"]:
            actor = body.get("agent_id")
        elif parts == ["tasks", "claim"]:
            actor = body.get("worker_id")
        elif len(parts) == 3 and parts[0] == "agents" and parts[2] == "heartbeat":
            actor = parts[1]
        elif len(parts) >= 3 and parts[0] == "tasks":
            if parts[2] in {"leases", "updates", "transition"}:
                actor = body.get("worker_id") if parts[2] == "leases" else body.get("actor")
            elif parts[2] == "artifacts":
                actor = body.get("provenance")
        supplied_id = self.headers.get("X-Agent-ID", "")
        supplied_token = self.headers.get("X-Agent-Token")
        if isinstance(actor, str) and actor and supplied_id == actor and self.server.store.authenticate_agent(actor, supplied_token):
            return True
        self._error(
            HTTPStatus.UNAUTHORIZED,
            "admin bearer or enrolled agent credential required",
            error_type="unauthorized",
        )
        return False

    def _require_get_auth(self, parts: list[str]) -> bool:
        if self._authorized():
            self._scoped_agent_id = None
            return True
        agent_id = self.headers.get("X-Agent-ID", "")
        if agent_id and self.server.store.authenticate_agent(agent_id, self.headers.get("X-Agent-Token")):
            try:
                if len(parts) == 4 and parts[0] == "tasks" and parts[2] == "artifacts":
                    self.server.store.assert_agent_can_access_artifact(parts[1], parts[3], worker_id=agent_id)
                elif len(parts) >= 2 and parts[0] == "tasks":
                    self.server.store.assert_agent_can_access_task(parts[1], worker_id=agent_id)
                elif len(parts) == 2 and parts[0] == "chains":
                    self.server.store.assert_agent_can_access_chain(parts[1], worker_id=agent_id)
            except AgentAccessError as exc:
                self._error(HTTPStatus.FORBIDDEN, str(exc), error_type="forbidden")
                return False
            self._scoped_agent_id = agent_id
            if parts and parts[0] in {"tasks", "agents", "chains"}:
                return True
        self._error(HTTPStatus.UNAUTHORIZED, "admin bearer or enrolled agent credential required", error_type="unauthorized")
        return False

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, JobNotFound):
            self._error(HTTPStatus.NOT_FOUND, str(exc), error_type="not_found")
        elif isinstance(exc, (IdempotencyConflict, LeaseConflict, LeaseNotFound, ChainConflict, ChainNotReady)):
            self._error(HTTPStatus.CONFLICT, str(exc), error_type=type(exc).__name__)
        elif isinstance(exc, (ProtocolError, ValueError, ChainInputError)):
            self._error(HTTPStatus.BAD_REQUEST, str(exc), error_type="protocol_error")
        elif isinstance(exc, AgentAccessError):
            self._error(HTTPStatus.FORBIDDEN, str(exc), error_type="forbidden")
        elif isinstance(exc, StoreError):
            self._error(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc), error_type=type(exc).__name__)
        else:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal coordinator error", error_type="internal_error")

    def _stream_events(self, task_id: str, *, after: int, timeout: float) -> None:
        """Serve a bounded SSE replay stream.

        The stream is deliberately bounded so an idle client cannot consume a
        coordinator thread forever. Reconnect with the last numeric SSE id;
        the ordinary JSON events endpoint remains the authoritative replay
        surface.
        """

        import time

        self.server.store.get(task_id)
        deadline = time.monotonic() + min(max(timeout, 0.0), MAX_STREAM_SECONDS)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        sequence = max(after, 0)
        while time.monotonic() <= deadline:
            events = self.server.store.events_since(task_id, sequence)
            if events:
                for event in events:
                    sequence += 1
                    payload = _json_bytes(event.to_dict()).decode("utf-8")
                    self.wfile.write(f"id: {sequence}\ndata: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    if event.state in TERMINAL_STATES:
                        return
                continue
            envelope = self.server.store.get(task_id)
            if envelope.state in TERMINAL_STATES:
                payload = _json_bytes({"task_id": task_id, "state": envelope.state.value}).decode("utf-8")
                self.wfile.write(f"event: snapshot\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                return
            time.sleep(0.1)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        parts = _path_parts(parsed.path)
        if parts == ["health"]:
            self._send(
                HTTPStatus.OK,
                {
                    "protocol": PROTOCOL_VERSION,
                    "healthy": True,
                    "server": "agent-relay",
                    "started_at": self.server.server_started_at,
                    "auth_required": bool(self.server.auth_token),
                    "tls": self.server.tls_enabled,
                    "durable_store": str(self.server.store.path),
                },
            )
            return
        if not self._require_get_auth(parts):
            return
        try:
            query = parse_qs(parsed.query)
            if parts == ["agents"]:
                self._send(
                    HTTPStatus.OK,
                    {
                        "protocol": PROTOCOL_VERSION,
                        "agents": [
                            item.to_dict()
                            for item in self.server.store.list_agents(
                                task_kind=query.get("task_kind", [None])[0],
                                capability=query.get("capability", [None])[0],
                                readiness=query.get("readiness", [None])[0],
                            )
                        ],
                    },
                )
                return
            if parts == ["tasks"]:
                state = query.get("state", [None])[0]
                if getattr(self, "_scoped_agent_id", None):
                    jobs = self.server.store.list_jobs_for_agent(self._scoped_agent_id)
                    if state:
                        jobs = [item for item in jobs if item.state.value == state]
                else:
                    jobs = self.server.store.list_jobs(state=state) if state else self.server.store.list_jobs()
                self._send(HTTPStatus.OK, {"protocol": PROTOCOL_VERSION, "tasks": [item.to_dict() for item in jobs]})
                return
            if len(parts) == 2 and parts[0] == "chains":
                self._send(HTTPStatus.OK, self.server.store.get_chain(parts[1]))
                return
            if len(parts) == 2 and parts[0] == "tasks":
                envelope = self.server.store.get(parts[1])
                self._send(HTTPStatus.OK, envelope.to_dict())
                return
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "events":
                after = int(query.get("after", ["0"])[0])
                events = self.server.store.events_since(parts[1], after)
                self._send(
                    HTTPStatus.OK,
                    {
                        "protocol": PROTOCOL_VERSION,
                        "task_id": parts[1],
                        "after": after,
                        "events": [event.to_dict() for event in events],
                    },
                )
                return
            if len(parts) == 4 and parts[0] == "tasks" and parts[2] == "events" and parts[3] == "stream":
                after = int(query.get("after", ["0"])[0])
                timeout = float(query.get("timeout", [str(MAX_STREAM_SECONDS)])[0])
                self._stream_events(parts[1], after=after, timeout=timeout)
                return
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "artifacts":
                artifacts = self.server.store.list_artifacts(parts[1])
                self._send(
                    HTTPStatus.OK,
                    {"protocol": PROTOCOL_VERSION, "task_id": parts[1], "artifacts": [item.to_dict() for item in artifacts]},
                )
                return
            if len(parts) == 4 and parts[0] == "tasks" and parts[2] == "artifacts":
                ref, content = self.server.store.get_artifact(parts[1], parts[3])
                self._send(
                    HTTPStatus.OK,
                    {
                        "protocol": PROTOCOL_VERSION,
                        "task_id": parts[1],
                        "artifact": ref.to_dict(),
                        "content_base64": base64.b64encode(content).decode("ascii"),
                    },
                )
                return
            self._error(HTTPStatus.NOT_FOUND, "endpoint not found", error_type="not_found")
        except Exception as exc:  # map domain failures without exposing internals
            self._handle_error(exc)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        parts = _path_parts(parsed.path)
        try:
            body = self._read_json()
            if not self._require_request_auth(parts, body):
                return
            if parts == ["tasks"]:
                envelope = _task_submission(body)
                stored, created = self.server.store.create_or_get(envelope)
                self._send(HTTPStatus.CREATED if created else HTTPStatus.OK, {
                    "protocol": PROTOCOL_VERSION,
                    "task_id": stored.task_id,
                    "state": stored.state.value,
                    "accepted_at": stored.created_at,
                    "created": created,
                    "envelope": stored.to_dict(),
                })
                return
            if parts == ["agents", "register"]:
                card = AgentCard.from_dict(body)
                supplied_id = self.headers.get("X-Agent-ID", "")
                supplied_token = self.headers.get("X-Agent-Token")
                if supplied_token is not None and supplied_id != card.agent_id:
                    raise ProtocolError("X-Agent-ID must match the registered Agent Card")
                self.server.store.register_agent(card, credential=supplied_token)
                self._send(HTTPStatus.CREATED, {"protocol": PROTOCOL_VERSION, "agent": card.to_dict()})
                return
            if len(parts) == 3 and parts[0] == "chains" and parts[2] == "steps":
                task = DelegationTask.from_dict(body.get("task", {}))
                allowed_states = body.get("allowed_predecessor_states", [JobState.SUCCEEDED.value])
                if isinstance(allowed_states, str):
                    allowed_states = [allowed_states]
                artifact_ids = body.get("parent_artifact_ids", [])
                messages = body.get("parent_messages", [])
                if isinstance(artifact_ids, str):
                    artifact_ids = [artifact_ids]
                if isinstance(messages, str):
                    messages = [messages]
                if not isinstance(allowed_states, list) or not isinstance(artifact_ids, list) or not isinstance(messages, list):
                    raise ProtocolError("chain states, parent artifact IDs, and parent messages must be lists")
                common = {
                    "chain_id": parts[1],
                    "step_id": body.get("step_id", ""),
                    "step_index": body.get("step_index", -1),
                    "task": task,
                    "predecessor_task_id": body.get("predecessor_task_id"),
                    "allowed_predecessor_states": tuple(allowed_states),
                    "parent_artifact_ids": tuple(artifact_ids),
                    "parent_messages": tuple(messages),
                    "idempotency_key": body.get("idempotency_key"),
                    "requested_by": body.get("requested_by", "orchestrator"),
                    "correlation_id": body.get("correlation_id"),
                    "workspace_policy": body.get("workspace_policy", {}),
                    "priority": body.get("priority", 0),
                    "deadline_at": body.get("deadline_at"),
                }
                if body.get("defer_until_ready") is True:
                    scheduled = self.server.store.schedule_chain_step(**common)
                    payload = scheduled.to_dict()
                    payload["protocol"] = PROTOCOL_VERSION
                    self._send(
                        HTTPStatus.ACCEPTED if scheduled.pending else (HTTPStatus.CREATED if scheduled.created else HTTPStatus.OK),
                        payload,
                    )
                else:
                    envelope, created = self.server.store.submit_chain_step(**common)
                    self._send(
                        HTTPStatus.CREATED if created else HTTPStatus.OK,
                        {
                            "protocol": PROTOCOL_VERSION,
                            "chain_id": parts[1],
                            "step_id": envelope.chain_step_id,
                            "created": created,
                            "pending": False,
                            "task_id": envelope.task_id,
                            "envelope": envelope.to_dict(),
                        },
                    )
                return
            if len(parts) == 3 and parts[0] == "chains" and parts[2] == "reconcile":
                if not self._authorized():
                    self._error(HTTPStatus.UNAUTHORIZED, "only the coordinator bearer can reconcile chains", error_type="unauthorized")
                    return
                result = self.server.store.reconcile_pending_chains(chain_id=parts[1])
                result["protocol"] = PROTOCOL_VERSION
                self._send(HTTPStatus.OK, result)
                return
            if parts == ["tasks", "claim"]:
                worker_id = body.get("worker_id", "")
                envelope_lease = self.server.store.claim_next(
                    worker_id=worker_id,
                    ttl_seconds=body.get("ttl_seconds", 60),
                )
                if envelope_lease is None:
                    self._send(
                        HTTPStatus.OK,
                        {
                            "protocol": PROTOCOL_VERSION,
                            "worker_id": worker_id,
                            "task": None,
                            "lease": None,
                            "reason": "no_compatible_work",
                        },
                    )
                    return
                envelope, lease = envelope_lease
                self._send(
                    HTTPStatus.OK,
                    {
                        "protocol": PROTOCOL_VERSION,
                        "worker_id": worker_id,
                        "envelope": envelope.to_dict(),
                        "lease": lease.to_dict(),
                    },
                )
                return
            if len(parts) == 3 and parts[0] == "agents" and parts[2] == "revoke":
                if not self._authorized():
                    self._error(HTTPStatus.UNAUTHORIZED, "only the coordinator bearer can revoke agents", error_type="unauthorized")
                    return
                card = self.server.store.revoke_agent(parts[1])
                self._send(HTTPStatus.OK, {"protocol": PROTOCOL_VERSION, "agent": card.to_dict()})
                return
            if len(parts) == 3 and parts[0] == "agents" and parts[2] == "heartbeat":
                readiness = body.get("readiness")
                if readiness is not None:
                    readiness = Readiness(readiness).value
                card = self.server.store.heartbeat_agent(
                    parts[1],
                    readiness=readiness,
                    metadata=body.get("metadata"),
                )
                self._send(HTTPStatus.OK, {"protocol": PROTOCOL_VERSION, "agent": card.to_dict()})
                return
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "artifacts":
                if not self._authorized():
                    self.server.store.assert_agent_owns_lease(
                        parts[1],
                        worker_id=body.get("provenance", ""),
                        lease_id=body.get("lease_id"),
                    )
                raw_content = body.get("content")
                if isinstance(raw_content, str):
                    content = raw_content.encode("utf-8")
                elif isinstance(body.get("content_base64"), str):
                    try:
                        content = base64.b64decode(body["content_base64"], validate=True)
                    except (ValueError, binascii.Error) as exc:
                        raise ProtocolError("content_base64 is invalid") from exc
                else:
                    raise ProtocolError("artifact requires content or content_base64")
                ref = self.server.store.put_artifact(
                    parts[1],
                    name=body.get("name", "artifact"),
                    content=content,
                    kind=body.get("kind", "file"),
                    media_type=body.get("media_type", "application/octet-stream"),
                    provenance=body.get("provenance", "worker"),
                    metadata=body.get("metadata", {}),
                )
                self._send(HTTPStatus.CREATED, {"protocol": PROTOCOL_VERSION, "artifact": ref.to_dict()})
                return
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "cancel":
                envelope = self.server.store.cancel(parts[1], actor=body.get("actor", "client"))
                self._send(HTTPStatus.OK, envelope.to_dict())
                return
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "resume":
                envelope = self.server.store.resume(parts[1], actor=body.get("actor", "client"))
                self._send(HTTPStatus.OK, envelope.to_dict())
                return
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "leases":
                # Admin/coordinator calls may intentionally schedule on behalf
                # of a worker. A scoped worker credential, however, must be
                # authorized by its enrolled Agent Card before lease grant.
                if self.headers.get("X-Agent-Token") and not self._authorized():
                    self.server.store.assert_agent_can_claim(
                        parts[1],
                        worker_id=body.get("worker_id", ""),
                    )
                envelope, lease = self.server.store.acquire_lease(
                    parts[1],
                    worker_id=body.get("worker_id", ""),
                    ttl_seconds=body.get("ttl_seconds", 60),
                )
                self._send(HTTPStatus.OK, {"protocol": PROTOCOL_VERSION, "envelope": envelope.to_dict(), "lease": lease.to_dict()})
                return
            if len(parts) == 4 and parts[0] == "tasks" and parts[2] == "leases" and parts[3] == "renew":
                lease = self.server.store.renew_lease(
                    parts[1],
                    lease_id=body.get("lease_id", ""),
                    worker_id=body.get("worker_id", ""),
                    ttl_seconds=body.get("ttl_seconds", 60),
                )
                self._send(HTTPStatus.OK, {"protocol": PROTOCOL_VERSION, "lease": lease.to_dict()})
                return
            if len(parts) == 4 and parts[0] == "tasks" and parts[2] == "leases" and parts[3] == "release":
                envelope = self.server.store.release_lease(
                    parts[1],
                    lease_id=body.get("lease_id", ""),
                    worker_id=body.get("worker_id", ""),
                )
                self._send(HTTPStatus.OK, envelope.to_dict())
                return
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "transition":
                receipt_raw = body.get("receipt")
                receipt = JobReceipt.from_dict(receipt_raw) if receipt_raw is not None else None
                envelope = self.server.store.transition(
                    parts[1],
                    body.get("state", ""),
                    actor=body.get("actor", ""),
                    lease_id=body.get("lease_id"),
                    reason=body.get("reason", ""),
                    evidence=body.get("evidence"),
                    receipt=receipt,
                    progress=body.get("progress"),
                    data=body.get("data"),
                )
                self._send(HTTPStatus.OK, envelope.to_dict())
                return
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "updates":
                envelope = self.server.store.record_update(
                    parts[1],
                    actor=body.get("actor", ""),
                    lease_id=body.get("lease_id"),
                    reason=body.get("reason", ""),
                    progress=body.get("progress"),
                    data=body.get("data"),
                )
                self._send(HTTPStatus.OK, envelope.to_dict())
                return
            self._error(HTTPStatus.NOT_FOUND, "endpoint not found", error_type="not_found")
        except Exception as exc:  # map domain failures without exposing internals
            self._handle_error(exc)


def create_server(
    *,
    host: str,
    port: int,
    database: str | Path,
    auth_token: str | None = None,
    tls_cert: str | Path | None = None,
    tls_key: str | Path | None = None,
) -> RelayHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"} and not auth_token:
        raise ControlPlaneError("non-loopback coordinator binds require an auth token")
    if (tls_cert is None) != (tls_key is None):
        raise ControlPlaneError("tls_cert and tls_key must be supplied together")
    token = auth_token if auth_token is not None else None
    store = RelayStore(database)
    # Reconcile durable recipes before accepting new work. This is a local
    # SQLite operation, so no distributed scheduler lease is required.
    store.reconcile_pending_chains()
    server = RelayHTTPServer((host, port), store, token)
    if tls_cert is not None and tls_key is not None:
        try:
            server.enable_tls(tls_cert, tls_key)
        except (OSError, ssl.SSLError, ValueError) as exc:
            server.server_close()
            raise ControlPlaneError(f"could not enable coordinator TLS: {exc}") from exc
    return server


def serve_forever(
    *,
    host: str,
    port: int,
    database: str | Path,
    auth_token: str | None = None,
    tls_cert: str | Path | None = None,
    tls_key: str | Path | None = None,
) -> None:
    server = create_server(
        host=host,
        port=port,
        database=database,
        auth_token=auth_token,
        tls_cert=tls_cert,
        tls_key=tls_key,
    )
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def request_json(
    base_url: str,
    method: str,
    path: str,
    *,
    payload: Mapping[str, Any] | None = None,
    auth_token: str | None = None,
    agent_id: str | None = None,
    agent_token: str | None = None,
    timeout: float = 10,
) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/" + path.lstrip("/")
    body = _json_bytes(payload) if payload is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    if agent_id:
        headers["X-Agent-ID"] = agent_id
    if agent_token:
        headers["X-Agent-Token"] = agent_token
    request = Request(url, method=method, data=body, headers=headers)
    try:
        context = _client_ssl_context(url)
        with urlopen(request, timeout=timeout, context=context) as response:
            raw = response.read(MAX_REQUEST_BYTES + 1)
            if len(raw) > MAX_REQUEST_BYTES:
                raise ControlPlaneError("coordinator response exceeded size limit")
            value = json.loads(raw.decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read(MAX_RESPONSE_TEXT).decode("utf-8", errors="replace")
        raise ControlPlaneError(f"HTTP {exc.code}: {detail}") from exc
    except (OSError, URLError, json.JSONDecodeError) as exc:
        raise ControlPlaneError(f"coordinator request failed: {exc}") from exc
    if not isinstance(value, dict):
        raise ControlPlaneError("coordinator returned a non-object JSON response")
    return value


def stream_events(
    base_url: str,
    path: str,
    *,
    auth_token: str | None = None,
    agent_id: str | None = None,
    agent_token: str | None = None,
    timeout: float = 35.0,
) -> Iterator[dict[str, Any]]:
    """Yield decoded events from the bounded coordinator SSE stream."""

    url = base_url.rstrip("/") + "/" + path.lstrip("/")
    headers = {"Accept": "text/event-stream"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    if agent_id:
        headers["X-Agent-ID"] = agent_id
    if agent_token:
        headers["X-Agent-Token"] = agent_token
    request = Request(url, method="GET", headers=headers)
    try:
        context = _client_ssl_context(url)
        with urlopen(request, timeout=timeout, context=context) as response:
            event_name = "message"
            event_id: int | None = None
            data_lines: list[str] = []
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    if data_lines:
                        try:
                            data = json.loads("\n".join(data_lines))
                        except json.JSONDecodeError as exc:
                            raise ControlPlaneError("coordinator returned invalid SSE JSON") from exc
                        if not isinstance(data, dict):
                            raise ControlPlaneError("coordinator returned a non-object SSE event")
                        yield {"event": event_name, "id": event_id, "data": data}
                    event_name = "message"
                    event_id = None
                    data_lines = []
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip() or "message"
                elif line.startswith("id:"):
                    try:
                        event_id = int(line[3:].strip())
                    except ValueError as exc:
                        raise ControlPlaneError("coordinator returned an invalid SSE event id") from exc
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
    except HTTPError as exc:
        detail = exc.read(MAX_RESPONSE_TEXT).decode("utf-8", errors="replace")
        raise ControlPlaneError(f"HTTP {exc.code}: {detail}") from exc
    except (OSError, URLError) as exc:
        raise ControlPlaneError(f"coordinator stream failed: {exc}") from exc


def default_database() -> Path:
    return Path.home() / ".agent-relay" / "relay.sqlite3"


def _client_ssl_context(url: str) -> ssl.SSLContext | None:
    """Build a verifying HTTPS context for coordinator requests.

    Self-signed or private-CA LAN certificates are supported by setting
    ``AR_RELAY_CA_CERT`` to the CA PEM file. There is intentionally no
    insecure-disable switch in the coordinator client.
    """

    if not url.lower().startswith("https://"):
        return None
    ca_cert = os.environ.get("AR_RELAY_CA_CERT")
    if ca_cert:
        return ssl.create_default_context(cafile=str(Path(ca_cert).expanduser().resolve()))
    return ssl.create_default_context()


def default_auth_token() -> str | None:
    # The caller can deliberately opt into an unauthenticated loopback server;
    # non-loopback use should pass AR_RELAY_AUTH_TOKEN explicitly.
    import os

    return os.environ.get("AR_RELAY_AUTH_TOKEN") or None


def default_agent_token() -> str | None:
    """Return the optional scoped worker credential from the environment."""

    import os

    return os.environ.get("AR_RELAY_AGENT_TOKEN") or None
