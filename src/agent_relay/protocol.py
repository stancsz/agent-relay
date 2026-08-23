"""Versioned Agent Relay protocol objects and lifecycle invariants.

The local adapters predate the control plane and already have a useful,
policy-rich :class:`DelegationTask`.  This module wraps that contract instead
of inventing a second task format.  The objects are deliberately dependency
free so a worker, coordinator, or small CLI client can share the same rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import re
from typing import Any, Mapping
from uuid import uuid4

from .task import DelegationTask


PROTOCOL_VERSION = "agent-relay/0.3"
SCHEMA_VERSION = "agent-relay-schema/0.1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_CHAIN_MESSAGES = 16
_MAX_CHAIN_MESSAGE_CHARS = 2_000


class ProtocolError(ValueError):
    """Raised when a protocol object or lifecycle transition is invalid."""


class JobState(str, Enum):
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


TERMINAL_STATES = frozenset(
    {
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.BLOCKED,
        JobState.CANCELLED,
        JobState.EXPIRED,
    }
)

_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.SUBMITTED: frozenset(
        {JobState.ACCEPTED, JobState.BLOCKED, JobState.CANCELLED, JobState.CANCEL_REQUESTED, JobState.EXPIRED}
    ),
    JobState.ACCEPTED: frozenset(
        {
            JobState.RUNNING,
            JobState.WAITING,
            JobState.FAILED,
            JobState.BLOCKED,
            JobState.CANCELLED,
            JobState.CANCEL_REQUESTED,
            JobState.EXPIRED,
        }
    ),
    JobState.RUNNING: frozenset(
        {
            JobState.WAITING,
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.BLOCKED,
            JobState.CANCEL_REQUESTED,
            JobState.EXPIRED,
        }
    ),
    JobState.WAITING: frozenset(
        {
            JobState.RUNNING,
            JobState.ACCEPTED,
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.BLOCKED,
            JobState.CANCEL_REQUESTED,
            JobState.EXPIRED,
        }
    ),
    JobState.CANCEL_REQUESTED: frozenset(
        {JobState.RUNNING, JobState.CANCELLED, JobState.BLOCKED, JobState.FAILED, JobState.EXPIRED}
    ),
    JobState.SUCCEEDED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.BLOCKED: frozenset(),
    JobState.CANCELLED: frozenset(),
    JobState.EXPIRED: frozenset(),
}


class Readiness(str, Enum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_deadline(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError("deadline_at must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtocolError("deadline_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ProtocolError("deadline_at must include a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _object(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolError(f"{field_name} must be an object")
    return dict(value)


def _text(value: Any, field_name: str, *, required: bool = True) -> str:
    if not isinstance(value, str) or (required and not value.strip()):
        requirement = "non-empty string" if required else "string"
        raise ProtocolError(f"{field_name} must be a {requirement}")
    return value


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ProtocolError(f"{field_name} must be a list of strings")
    result = tuple(_text(item, field_name) for item in value)
    if len(set(result)) != len(result):
        raise ProtocolError(f"{field_name} must not contain duplicates")
    return result


def _json_roundtrip(value: Any, field_name: str) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"{field_name} must contain JSON-compatible values") from exc
    return value


@dataclass(frozen=True)
class AgentCard:
    """A worker's identity, capability boundary, and readiness claim."""

    agent_id: str
    name: str
    readiness: Readiness = Readiness.UNKNOWN
    capabilities: tuple[str, ...] = ()
    task_kinds: tuple[str, ...] = ()
    transports: tuple[str, ...] = ()
    protocol_version: str = PROTOCOL_VERSION
    workspace: Mapping[str, Any] = field(default_factory=dict)
    artifact_limits: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _text(self.agent_id, "agent_id")
        _text(self.name, "name")
        if not isinstance(self.readiness, Readiness):
            try:
                object.__setattr__(self, "readiness", Readiness(self.readiness))
            except ValueError as exc:
                raise ProtocolError("readiness must be ready, degraded, blocked, or unknown") from exc
        _text(self.protocol_version, "protocol_version")
        for name, value in (
            ("capabilities", self.capabilities),
            ("task_kinds", self.task_kinds),
            ("transports", self.transports),
        ):
            if not isinstance(value, tuple) or any(not isinstance(item, str) or not item for item in value):
                raise ProtocolError(f"{name} must be a tuple of non-empty strings")
        for name, value in (
            ("workspace", self.workspace),
            ("artifact_limits", self.artifact_limits),
            ("metadata", self.metadata),
        ):
            _json_roundtrip(_object(value, name), name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "protocol": self.protocol_version,
            "agent_id": self.agent_id,
            "name": self.name,
            "readiness": self.readiness.value,
            "capabilities": list(self.capabilities),
            "task_kinds": list(self.task_kinds),
            "transports": list(self.transports),
            "workspace": dict(self.workspace),
            "artifact_limits": dict(self.artifact_limits),
            "metadata": dict(self.metadata),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AgentCard":
        raw = _object(value, "agent_card")
        if raw.get("schema") not in {None, SCHEMA_VERSION}:
            raise ProtocolError(f"unsupported agent card schema: {raw.get('schema')!r}")
        return cls(
            agent_id=raw.get("agent_id", ""),
            name=raw.get("name", ""),
            readiness=raw.get("readiness", Readiness.UNKNOWN.value),
            capabilities=_string_tuple(raw.get("capabilities", ()), "capabilities"),
            task_kinds=_string_tuple(raw.get("task_kinds", ()), "task_kinds"),
            transports=_string_tuple(raw.get("transports", ()), "transports"),
            protocol_version=raw.get("protocol", PROTOCOL_VERSION),
            workspace=_object(raw.get("workspace", {}), "workspace"),
            artifact_limits=_object(raw.get("artifact_limits", {}), "artifact_limits"),
            metadata=_object(raw.get("metadata", {}), "metadata"),
            updated_at=raw.get("updated_at", utc_now()),
        )


@dataclass(frozen=True)
class ArtifactRef:
    """A bounded, hash-addressed output attached to a job receipt."""

    artifact_id: str
    name: str
    sha256: str
    size_bytes: int
    kind: str = "file"
    media_type: str = "application/octet-stream"
    provenance: str = "worker"
    uri: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("artifact_id", "name", "kind", "media_type", "provenance"):
            _text(getattr(self, name), name)
        if not isinstance(self.sha256, str) or not _SHA256.fullmatch(self.sha256):
            raise ProtocolError("sha256 must be a lowercase 64-character hex digest")
        if not isinstance(self.size_bytes, int) or isinstance(self.size_bytes, bool) or self.size_bytes < 0:
            raise ProtocolError("size_bytes must be a non-negative integer")
        if self.uri is not None:
            _text(self.uri, "uri")
        _json_roundtrip(_object(self.metadata, "metadata"), "metadata")

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "name": self.name,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "kind": self.kind,
            "media_type": self.media_type,
            "provenance": self.provenance,
            "uri": self.uri,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactRef":
        raw = _object(value, "artifact")
        return cls(
            artifact_id=raw.get("artifact_id", ""),
            name=raw.get("name", ""),
            sha256=raw.get("sha256", ""),
            size_bytes=raw.get("size_bytes", -1),
            kind=raw.get("kind", "file"),
            media_type=raw.get("media_type", "application/octet-stream"),
            provenance=raw.get("provenance", "worker"),
            uri=raw.get("uri"),
            metadata=_object(raw.get("metadata", {}), "metadata"),
        )


@dataclass(frozen=True)
class MessageUpdate:
    """A durable lifecycle/progress update, replayable after reconnect."""

    event_id: str
    task_id: str
    state: JobState
    actor: str
    timestamp: str
    reason: str = ""
    correlation_id: str = ""
    progress: float | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("event_id", "task_id", "actor", "timestamp"):
            _text(getattr(self, name), name)
        if not isinstance(self.state, JobState):
            try:
                object.__setattr__(self, "state", JobState(self.state))
            except ValueError as exc:
                raise ProtocolError("event state is not a canonical job state") from exc
        if self.progress is not None and (
            not isinstance(self.progress, (int, float)) or isinstance(self.progress, bool)
            or not 0 <= self.progress <= 1
        ):
            raise ProtocolError("progress must be between 0 and 1")
        _json_roundtrip(_object(self.data, "data"), "data")

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "task_id": self.task_id,
            "state": self.state.value,
            "actor": self.actor,
            "timestamp": self.timestamp,
            "reason": self.reason,
            "correlation_id": self.correlation_id,
            "progress": self.progress,
            "data": dict(self.data),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MessageUpdate":
        raw = _object(value, "event")
        return cls(
            event_id=raw.get("event_id", ""),
            task_id=raw.get("task_id", ""),
            state=raw.get("state", ""),
            actor=raw.get("actor", ""),
            timestamp=raw.get("timestamp", ""),
            reason=raw.get("reason", ""),
            correlation_id=raw.get("correlation_id", ""),
            progress=raw.get("progress"),
            data=_object(raw.get("data", {}), "data"),
        )


@dataclass(frozen=True)
class JobReceipt:
    """Terminal proof bundle; every terminal state must carry one."""

    receipt_id: str
    task_id: str
    final_state: JobState
    actor: str
    completed_at: str
    evidence: Mapping[str, Any]
    artifacts: tuple[ArtifactRef, ...] = ()
    verification: tuple[Mapping[str, Any], ...] = ()
    workspace: Mapping[str, Any] = field(default_factory=dict)
    summary: str = ""

    def __post_init__(self) -> None:
        for name in ("receipt_id", "task_id", "actor", "completed_at"):
            _text(getattr(self, name), name)
        if not isinstance(self.final_state, JobState):
            try:
                object.__setattr__(self, "final_state", JobState(self.final_state))
            except ValueError as exc:
                raise ProtocolError("receipt final_state is not canonical") from exc
        if self.final_state not in TERMINAL_STATES:
            raise ProtocolError("a receipt can only describe a terminal state")
        if not _object(self.evidence, "evidence"):
            raise ProtocolError("terminal evidence must not be empty")
        for name, value in (("verification", self.verification), ("artifacts", self.artifacts)):
            if not isinstance(value, tuple):
                raise ProtocolError(f"{name} must be a tuple")
        if any(not isinstance(item, ArtifactRef) for item in self.artifacts):
            raise ProtocolError("artifacts must contain ArtifactRef objects")
        if any(not isinstance(item, Mapping) for item in self.verification):
            raise ProtocolError("verification must contain objects")
        _json_roundtrip(_object(self.workspace, "workspace"), "workspace")

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL_VERSION,
            "receipt_id": self.receipt_id,
            "task_id": self.task_id,
            "final_state": self.final_state.value,
            "actor": self.actor,
            "completed_at": self.completed_at,
            "evidence": dict(self.evidence),
            "artifacts": [item.to_dict() for item in self.artifacts],
            "verification": [dict(item) for item in self.verification],
            "workspace": dict(self.workspace),
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "JobReceipt":
        raw = _object(value, "receipt")
        if raw.get("protocol") not in {None, PROTOCOL_VERSION}:
            raise ProtocolError(f"unsupported receipt protocol: {raw.get('protocol')!r}")
        verification = raw.get("verification", ())
        if not isinstance(verification, (list, tuple)):
            raise ProtocolError("verification must be a list")
        return cls(
            receipt_id=raw.get("receipt_id", ""),
            task_id=raw.get("task_id", ""),
            final_state=raw.get("final_state", ""),
            actor=raw.get("actor", ""),
            completed_at=raw.get("completed_at", ""),
            evidence=_object(raw.get("evidence", {}), "evidence"),
            artifacts=tuple(ArtifactRef.from_dict(item) for item in raw.get("artifacts", ())),
            verification=tuple(_object(item, "verification item") for item in verification),
            workspace=_object(raw.get("workspace", {}), "workspace"),
            summary=raw.get("summary", ""),
        )


@dataclass(frozen=True)
class JobEnvelope:
    """Canonical durable task envelope and replayable lifecycle history."""

    task: DelegationTask
    idempotency_key: str
    requested_by: str
    correlation_id: str
    priority: int = 0
    deadline_at: str | None = None
    state: JobState = JobState.SUBMITTED
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    worker_id: str | None = None
    lease_id: str | None = None
    lease_expires_at: str | None = None
    chain_id: str | None = None
    chain_step_id: str | None = None
    chain_step_index: int | None = None
    predecessor_task_id: str | None = None
    parent_artifacts: tuple[ArtifactRef, ...] = ()
    parent_messages: tuple[str, ...] = ()
    workspace_policy: Mapping[str, Any] = field(default_factory=dict)
    events: tuple[MessageUpdate, ...] = ()
    receipt: JobReceipt | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.idempotency_key, "idempotency_key")
        _text(self.requested_by, "requested_by")
        _text(self.correlation_id, "correlation_id")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool) or not -1000 <= self.priority <= 1000:
            raise ProtocolError("priority must be an integer between -1000 and 1000")
        object.__setattr__(self, "deadline_at", _normalize_deadline(self.deadline_at))
        if not isinstance(self.state, JobState):
            try:
                object.__setattr__(self, "state", JobState(self.state))
            except ValueError as exc:
                raise ProtocolError("state is not a canonical job state") from exc
        if not isinstance(self.events, tuple):
            raise ProtocolError("events must be a tuple")
        previous_state: JobState | None = None
        for index, event in enumerate(self.events):
            if not isinstance(event, MessageUpdate):
                raise ProtocolError("events must contain MessageUpdate objects")
            if event.task_id != self.task.task_id:
                raise ProtocolError("event task_id does not match envelope task")
            if index == 0 and event.state is not JobState.SUBMITTED:
                raise ProtocolError("the first lifecycle event must be submitted")
            if (
                previous_state is not None
                and event.state is not previous_state
                and event.state not in _TRANSITIONS[previous_state]
            ):
                raise ProtocolError(
                    f"event history contains invalid transition "
                    f"{previous_state.value} -> {event.state.value}"
                )
            previous_state = event.state
        if self.events and self.events[-1].state is not self.state:
            raise ProtocolError("last lifecycle event does not match envelope state")
        if self.receipt is not None and self.receipt.task_id != self.task.task_id:
            raise ProtocolError("receipt task_id does not match envelope task")
        if self.state in TERMINAL_STATES and self.receipt is None:
            raise ProtocolError("terminal envelope requires a receipt")
        chain_fields = (self.chain_id, self.chain_step_id, self.chain_step_index)
        if any(value is not None for value in chain_fields):
            if not isinstance(self.chain_id, str) or not self.chain_id.strip():
                raise ProtocolError("chain_id is required for a chained envelope")
            if not isinstance(self.chain_step_id, str) or not self.chain_step_id.strip():
                raise ProtocolError("chain_step_id is required for a chained envelope")
            if (
                not isinstance(self.chain_step_index, int)
                or isinstance(self.chain_step_index, bool)
                or self.chain_step_index < 0
            ):
                raise ProtocolError("chain_step_index must be a non-negative integer")
            if self.chain_step_index == 0 and self.predecessor_task_id is not None:
                raise ProtocolError("the first chain step cannot have a predecessor")
            if self.chain_step_index > 0 and (
                not isinstance(self.predecessor_task_id, str)
                or not self.predecessor_task_id.strip()
            ):
                raise ProtocolError("non-first chain steps require a predecessor_task_id")
        elif self.predecessor_task_id is not None:
            raise ProtocolError("predecessor_task_id requires chain fields")
        if not isinstance(self.parent_artifacts, tuple) or any(
            not isinstance(item, ArtifactRef) for item in self.parent_artifacts
        ):
            raise ProtocolError("parent_artifacts must contain ArtifactRef objects")
        if not isinstance(self.parent_messages, tuple) or any(
            not isinstance(item, str) or not item.strip() or len(item) > _MAX_CHAIN_MESSAGE_CHARS
            for item in self.parent_messages
        ):
            raise ProtocolError(
                f"parent_messages must contain non-empty strings of at most {_MAX_CHAIN_MESSAGE_CHARS} characters"
            )
        if len(self.parent_messages) > _MAX_CHAIN_MESSAGES:
            raise ProtocolError(f"parent_messages may contain at most {_MAX_CHAIN_MESSAGES} items")
        if self.chain_id is None and (self.parent_artifacts or self.parent_messages):
            raise ProtocolError("parent inputs require a chained envelope")
        if self.predecessor_task_id is None and (self.parent_artifacts or self.parent_messages):
            raise ProtocolError("parent inputs require a predecessor_task_id")
        _json_roundtrip(_object(self.workspace_policy, "workspace_policy"), "workspace_policy")
        _json_roundtrip(_object(self.metadata, "metadata"), "metadata")

    @property
    def task_id(self) -> str:
        return self.task.task_id

    @classmethod
    def new(
        cls,
        task: DelegationTask,
        *,
        idempotency_key: str | None = None,
        requested_by: str = "client",
        correlation_id: str | None = None,
        priority: int = 0,
        deadline_at: str | None = None,
        workspace_policy: Mapping[str, Any] | None = None,
        chain_id: str | None = None,
        chain_step_id: str | None = None,
        chain_step_index: int | None = None,
        predecessor_task_id: str | None = None,
        parent_artifacts: tuple[ArtifactRef, ...] = (),
        parent_messages: tuple[str, ...] = (),
    ) -> "JobEnvelope":
        now = utc_now()
        envelope = cls(
            task=task,
            idempotency_key=idempotency_key or task.task_id,
            requested_by=requested_by,
            correlation_id=correlation_id or _new_id("corr"),
            priority=priority,
            deadline_at=deadline_at,
            created_at=now,
            updated_at=now,
            chain_id=chain_id,
            chain_step_id=chain_step_id,
            chain_step_index=chain_step_index,
            predecessor_task_id=predecessor_task_id,
            parent_artifacts=tuple(parent_artifacts),
            parent_messages=tuple(parent_messages),
            workspace_policy=dict(workspace_policy or {}),
        )
        return envelope._append_event(
            state=JobState.SUBMITTED,
            actor=requested_by,
            reason="task submitted",
            timestamp=now,
        )

    def _append_event(
        self,
        *,
        state: JobState,
        actor: str,
        reason: str,
        timestamp: str,
        progress: float | None = None,
        data: Mapping[str, Any] | None = None,
        envelope_state: JobState | None = None,
        receipt: JobReceipt | None = None,
    ) -> "JobEnvelope":
        event = MessageUpdate(
            event_id=_new_id("evt"),
            task_id=self.task_id,
            state=state,
            actor=actor,
            timestamp=timestamp,
            reason=reason,
            correlation_id=self.correlation_id,
            progress=progress,
            data=dict(data or {}),
        )
        return replace(
            self,
            state=envelope_state or self.state,
            receipt=receipt if envelope_state in TERMINAL_STATES else self.receipt,
            updated_at=timestamp,
            events=(*self.events, event),
        )

    def transition(
        self,
        target: JobState | str,
        *,
        actor: str,
        reason: str,
        evidence: Mapping[str, Any] | None = None,
        receipt: JobReceipt | None = None,
        timestamp: str | None = None,
        progress: float | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> "JobEnvelope":
        try:
            target_state = target if isinstance(target, JobState) else JobState(target)
        except ValueError as exc:
            raise ProtocolError(f"unknown target state: {target!r}") from exc
        if target_state not in _TRANSITIONS[self.state]:
            raise ProtocolError(f"invalid transition {self.state.value} -> {target_state.value}")
        _text(actor, "actor")
        _text(reason, "reason")
        when = timestamp or utc_now()
        if target_state in TERMINAL_STATES:
            if not evidence:
                raise ProtocolError("terminal transition requires non-empty evidence")
            if receipt is None:
                raise ProtocolError("terminal transition requires a receipt")
            if receipt.task_id != self.task_id or receipt.final_state is not target_state:
                raise ProtocolError("receipt does not match terminal transition")
        elif receipt is not None or evidence:
            raise ProtocolError("evidence and receipt are only valid for terminal transitions")
        return self._append_event(
            state=target_state,
            actor=actor,
            reason=reason,
            timestamp=when,
            progress=progress,
            data=data,
            envelope_state=target_state,
            receipt=receipt if target_state in TERMINAL_STATES else None,
        )

    def assign_lease(
        self,
        *,
        worker_id: str,
        lease_id: str,
        lease_expires_at: str,
        actor: str = "coordinator",
    ) -> "JobEnvelope":
        if self.state not in {JobState.SUBMITTED, JobState.ACCEPTED, JobState.WAITING}:
            raise ProtocolError(f"cannot assign a lease while task is {self.state.value}")
        _text(worker_id, "worker_id")
        _text(lease_id, "lease_id")
        _text(lease_expires_at, "lease_expires_at")
        next_envelope = replace(
            self,
            worker_id=worker_id,
            lease_id=lease_id,
            lease_expires_at=lease_expires_at,
        )
        return next_envelope._append_event(
            state=self.state,
            actor=actor,
            reason="lease assigned",
            timestamp=utc_now(),
            data={"worker_id": worker_id, "lease_id": lease_id, "lease_expires_at": lease_expires_at},
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema": SCHEMA_VERSION,
            "protocol": PROTOCOL_VERSION,
            "task": self.task.to_dict(),
            "idempotency_key": self.idempotency_key,
            "requested_by": self.requested_by,
            "correlation_id": self.correlation_id,
            "priority": self.priority,
            "deadline_at": self.deadline_at,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "worker_id": self.worker_id,
            "lease_id": self.lease_id,
            "lease_expires_at": self.lease_expires_at,
            "chain_id": self.chain_id,
            "chain_step_id": self.chain_step_id,
            "chain_step_index": self.chain_step_index,
            "predecessor_task_id": self.predecessor_task_id,
            "parent_artifacts": [item.to_dict() for item in self.parent_artifacts],
            "parent_messages": list(self.parent_messages),
            "workspace_policy": dict(self.workspace_policy),
            "events": [event.to_dict() for event in self.events],
            "receipt": self.receipt.to_dict() if self.receipt else None,
            "metadata": dict(self.metadata),
        }
        payload["envelope_sha256"] = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "JobEnvelope":
        raw = _object(value, "job envelope")
        if raw.get("schema") not in {None, SCHEMA_VERSION}:
            raise ProtocolError(f"unsupported job schema: {raw.get('schema')!r}")
        if raw.get("protocol") not in {None, PROTOCOL_VERSION}:
            raise ProtocolError(f"unsupported job protocol: {raw.get('protocol')!r}")
        task = DelegationTask.from_dict(_object(raw.get("task", {}), "task"))
        if raw.get("envelope_sha256"):
            unsigned = dict(raw)
            expected = unsigned.pop("envelope_sha256")
            actual = hashlib.sha256(
                json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            if expected != actual:
                raise ProtocolError("envelope_sha256 does not match payload")
        events_raw = raw.get("events", ())
        if not isinstance(events_raw, (list, tuple)):
            raise ProtocolError("events must be a list")
        events = tuple(MessageUpdate.from_dict(item) for item in events_raw)
        receipt_raw = raw.get("receipt")
        return cls(
            task=task,
            idempotency_key=raw.get("idempotency_key", ""),
            requested_by=raw.get("requested_by", ""),
            correlation_id=raw.get("correlation_id", ""),
            priority=raw.get("priority", 0),
            deadline_at=raw.get("deadline_at"),
            state=raw.get("state", ""),
            created_at=raw.get("created_at", ""),
            updated_at=raw.get("updated_at", ""),
            worker_id=raw.get("worker_id"),
            lease_id=raw.get("lease_id"),
            lease_expires_at=raw.get("lease_expires_at"),
            chain_id=raw.get("chain_id"),
            chain_step_id=raw.get("chain_step_id"),
            chain_step_index=raw.get("chain_step_index"),
            predecessor_task_id=raw.get("predecessor_task_id"),
            parent_artifacts=tuple(
                ArtifactRef.from_dict(item) for item in raw.get("parent_artifacts", ())
            ),
            parent_messages=tuple(raw.get("parent_messages", ())),
            workspace_policy=_object(raw.get("workspace_policy", {}), "workspace_policy"),
            events=events,
            receipt=JobReceipt.from_dict(receipt_raw) if receipt_raw is not None else None,
            metadata=_object(raw.get("metadata", {}), "metadata"),
        )


def protocol_digest(value: Mapping[str, Any]) -> str:
    """Return a stable digest for a protocol payload without transport noise."""

    unsigned = dict(value)
    unsigned.pop("envelope_sha256", None)
    return hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
