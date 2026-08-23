"""Durable, dependency-free SQLite state for the Agent Relay control plane."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator, Mapping
from uuid import uuid4

from .protocol import (
    AgentCard,
    ArtifactRef,
    JobEnvelope,
    JobReceipt,
    JobState,
    MessageUpdate,
    Readiness,
    TERMINAL_STATES,
    ProtocolError,
    utc_now,
)
from .task import DelegationTask


class StoreError(RuntimeError):
    """Base class for durable control-plane errors."""


class JobNotFound(StoreError):
    pass


class IdempotencyConflict(StoreError):
    pass


class LeaseConflict(StoreError):
    pass


class LeaseNotFound(StoreError):
    pass


class VersionConflict(StoreError):
    pass


class AgentAuthError(StoreError):
    pass


class AgentAccessError(StoreError):
    """Raised when a valid enrolled worker lacks task/artifact access."""

    pass


class AgentCapabilityError(StoreError):
    """Raised when an enrolled worker cannot satisfy a task policy."""

    pass


class ChainConflict(StoreError):
    """Raised when a chain step would violate its linear/idempotent contract."""

    pass


class ChainNotReady(StoreError):
    """Raised when a predecessor has not reached an explicitly allowed state."""

    pass


class ChainInputError(StoreError):
    """Raised when a child requests undeclared or invalid parent inputs."""

    pass


@dataclass(frozen=True)
class LeaseGrant:
    task_id: str
    lease_id: str
    worker_id: str
    expires_at: str
    renewed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "lease_id": self.lease_id,
            "worker_id": self.worker_id,
            "expires_at": self.expires_at,
            "renewed": self.renewed,
        }


@dataclass(frozen=True)
class ChainSchedule:
    """The durable outcome of scheduling a chain step."""

    chain_id: str
    step_id: str
    step_index: int
    created: bool
    pending: bool
    envelope: JobEnvelope | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "step_id": self.step_id,
            "step_index": self.step_index,
            "created": self.created,
            "pending": self.pending,
            "status": "pending" if self.pending else ("created" if self.created else "existing"),
            "error": self.error,
            "envelope": self.envelope.to_dict() if self.envelope is not None else None,
        }


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StoreError(f"invalid lease timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise StoreError("lease timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def _expires_after(ttl_seconds: float) -> str:
    if not isinstance(ttl_seconds, (int, float)) or isinstance(ttl_seconds, bool) or ttl_seconds <= 0:
        raise StoreError("lease ttl must be greater than zero")
    return (
        datetime.now(timezone.utc) + timedelta(seconds=float(ttl_seconds))
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _expired(value: str, *, now: datetime | None = None) -> bool:
    return _parse_time(value) <= (now or datetime.now(timezone.utc))


class RelayStore:
    """SQLite-backed job, event, lease, and agent registry.

    Each operation opens a short-lived connection and commits one transaction.
    That keeps the store safe across coordinator restarts and allows a worker
    process to inspect the same file without sharing Python connection state.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = sqlite3.connect(
                self.path,
                timeout=10,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("PRAGMA journal_mode = WAL")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                if write:
                    connection.commit()
            except Exception:
                if write:
                    connection.rollback()
                raise
            finally:
                connection.close()

    def _initialize(self) -> None:
        with self._connection(write=True) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    task_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES jobs(task_id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    UNIQUE(task_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS events_task_sequence
                    ON events(task_id, sequence);
                CREATE TABLE IF NOT EXISTS leases (
                    task_id TEXT PRIMARY KEY REFERENCES jobs(task_id) ON DELETE CASCADE,
                    lease_id TEXT NOT NULL UNIQUE,
                    worker_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    updated_at TEXT NOT NULL,
                    card_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_credentials (
                    agent_id TEXT PRIMARY KEY REFERENCES agents(agent_id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES jobs(task_id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    uri TEXT,
                    metadata_json TEXT NOT NULL,
                    content BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(task_id, sha256, name)
                );
                CREATE INDEX IF NOT EXISTS artifacts_task ON artifacts(task_id, created_at);
                CREATE TABLE IF NOT EXISTS chains (
                    chain_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    policy_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chain_steps (
                    chain_id TEXT NOT NULL REFERENCES chains(chain_id) ON DELETE CASCADE,
                    step_id TEXT NOT NULL,
                    step_index INTEGER NOT NULL,
                    task_id TEXT NOT NULL UNIQUE REFERENCES jobs(task_id) ON DELETE CASCADE,
                    predecessor_task_id TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_sha256 TEXT NOT NULL DEFAULT '',
                    allowed_states_json TEXT NOT NULL DEFAULT '["succeeded"]',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(chain_id, step_id),
                    UNIQUE(chain_id, step_index)
                );
                CREATE INDEX IF NOT EXISTS chain_steps_chain_index
                    ON chain_steps(chain_id, step_index);
                CREATE TABLE IF NOT EXISTS pending_chain_steps (
                    chain_id TEXT NOT NULL REFERENCES chains(chain_id) ON DELETE CASCADE,
                    step_id TEXT NOT NULL,
                    step_index INTEGER NOT NULL,
                    task_id TEXT NOT NULL,
                    predecessor_task_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_sha256 TEXT NOT NULL DEFAULT '',
                    request_json TEXT NOT NULL,
                    allowed_states_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    materialized_task_id TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    resolved_at TEXT,
                    PRIMARY KEY(chain_id, step_id),
                    UNIQUE(chain_id, step_index)
                );
                CREATE INDEX IF NOT EXISTS pending_chain_steps_predecessor
                    ON pending_chain_steps(predecessor_task_id, status);
                """
            )
            chain_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(chain_steps)").fetchall()
            }
            if "allowed_states_json" not in chain_columns:
                connection.execute(
                    "ALTER TABLE chain_steps ADD COLUMN allowed_states_json TEXT NOT NULL DEFAULT '[\"succeeded\"]'"
                )
            if "request_sha256" not in chain_columns:
                connection.execute(
                    "ALTER TABLE chain_steps ADD COLUMN request_sha256 TEXT NOT NULL DEFAULT ''"
                )
            pending_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(pending_chain_steps)").fetchall()
            }
            if "request_sha256" not in pending_columns:
                connection.execute(
                    "ALTER TABLE pending_chain_steps ADD COLUMN request_sha256 TEXT NOT NULL DEFAULT ''"
                )

    @staticmethod
    def _dump(value: Mapping[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _load(value: str) -> dict[str, Any]:
        try:
            raw = json.loads(value)
        except json.JSONDecodeError as exc:
            raise StoreError("stored JSON is corrupt") from exc
        if not isinstance(raw, dict):
            raise StoreError("stored payload is not an object")
        return raw

    @classmethod
    def _chain_request_sha256(
        cls,
        *,
        chain_id: str,
        step_id: str,
        step_index: int,
        task: DelegationTask,
        predecessor_task_id: str | None,
        allowed_states: tuple[JobState, ...],
        parent_artifact_ids: tuple[str, ...],
        parent_messages: tuple[str, ...],
        idempotency_key: str,
        requested_by: str,
        correlation_id: str | None,
        workspace_policy: Mapping[str, Any] | None,
        priority: int,
        deadline_at: str | None,
    ) -> str:
        request = {
            "chain_id": chain_id,
            "step_id": step_id,
            "step_index": step_index,
            "task": task.to_dict(),
            "predecessor_task_id": predecessor_task_id,
            "allowed_predecessor_states": [state.value for state in allowed_states],
            "parent_artifact_ids": list(parent_artifact_ids),
            "parent_messages": list(parent_messages),
            "idempotency_key": idempotency_key,
            "requested_by": requested_by,
            "correlation_id": correlation_id,
            "workspace_policy": dict(workspace_policy or {}),
            "priority": priority,
            "deadline_at": deadline_at,
        }
        return hashlib.sha256(cls._dump(request).encode("utf-8")).hexdigest()

    @staticmethod
    def _row_envelope(row: sqlite3.Row) -> JobEnvelope:
        try:
            return JobEnvelope.from_dict(RelayStore._load(row["payload_json"]))
        except ProtocolError as exc:
            raise StoreError(f"stored job {row['task_id']} violates protocol: {exc}") from exc

    @staticmethod
    def _insert_events(connection: sqlite3.Connection, envelope: JobEnvelope) -> None:
        for sequence, event in enumerate(envelope.events, start=1):
            connection.execute(
                """
                INSERT OR IGNORE INTO events
                    (event_id, task_id, sequence, state, timestamp, actor, event_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    envelope.task_id,
                    sequence,
                    event.state.value,
                    event.timestamp,
                    event.actor,
                    RelayStore._dump(event.to_dict()),
                ),
            )

    @staticmethod
    def _persist(
        connection: sqlite3.Connection,
        envelope: JobEnvelope,
        *,
        expected_version: int | None = None,
    ) -> int:
        payload = RelayStore._dump(envelope.to_dict())
        if expected_version is None:
            connection.execute(
                """
                INSERT INTO jobs
                    (task_id, idempotency_key, state, version, created_at, updated_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    envelope.task_id,
                    envelope.idempotency_key,
                    envelope.state.value,
                    0,
                    envelope.created_at,
                    envelope.updated_at,
                    payload,
                ),
            )
            version = 0
        else:
            version = expected_version + 1
            result = connection.execute(
                """
                UPDATE jobs
                   SET state = ?, version = ?, updated_at = ?, payload_json = ?
                 WHERE task_id = ? AND version = ?
                """,
                (
                    envelope.state.value,
                    version,
                    envelope.updated_at,
                    payload,
                    envelope.task_id,
                    expected_version,
                ),
            )
            if result.rowcount != 1:
                raise VersionConflict(f"job {envelope.task_id} changed while it was being updated")
        RelayStore._insert_events(connection, envelope)
        return version

    def create_or_get(self, envelope: JobEnvelope) -> tuple[JobEnvelope, bool]:
        """Insert once by idempotency key; return ``(envelope, created)``."""

        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?",
                (envelope.idempotency_key,),
            ).fetchone()
            if row is not None:
                existing = self._row_envelope(row)
                if existing.task_id != envelope.task_id:
                    raise IdempotencyConflict(
                        f"idempotency key {envelope.idempotency_key!r} belongs to {existing.task_id}"
                    )
                return existing, False
            task_row = connection.execute(
                "SELECT task_id FROM jobs WHERE task_id = ?",
                (envelope.task_id,),
            ).fetchone()
            if task_row is not None:
                raise IdempotencyConflict(f"task_id {envelope.task_id!r} already exists")
            self._persist(connection, envelope)
            return envelope, True

    def submit_chain_step(
        self,
        *,
        chain_id: str,
        step_id: str,
        step_index: int,
        task: Any,
        predecessor_task_id: str | None = None,
        allowed_predecessor_states: tuple[JobState | str, ...] = (JobState.SUCCEEDED,),
        parent_artifact_ids: tuple[str, ...] = (),
        parent_messages: tuple[str, ...] = (),
        idempotency_key: str | None = None,
        requested_by: str = "orchestrator",
        correlation_id: str | None = None,
        workspace_policy: Mapping[str, Any] | None = None,
        priority: int = 0,
        deadline_at: str | None = None,
    ) -> tuple[JobEnvelope, bool]:
        """Create one linear chain step after its predecessor is eligible.

        The operation is one SQLite transaction. Repeating the same
        ``chain_id``/``step_id`` or idempotency key returns the original child
        envelope; it never creates a second logical step. Parent artifacts are
        references only and parent messages are bounded strings, so a child
        receives no implicit transcript or repository context.
        """

        if not isinstance(chain_id, str) or not chain_id.strip():
            raise ChainInputError("chain_id must be a non-empty string")
        if not isinstance(step_id, str) or not step_id.strip():
            raise ChainInputError("step_id must be a non-empty string")
        if not isinstance(step_index, int) or isinstance(step_index, bool) or step_index < 0:
            raise ChainInputError("step_index must be a non-negative integer")
        if not isinstance(task, DelegationTask):
            raise ChainInputError("task must be a DelegationTask")
        if predecessor_task_id is not None and (
            not isinstance(predecessor_task_id, str) or not predecessor_task_id.strip()
        ):
            raise ChainInputError("predecessor_task_id must be a non-empty string")
        try:
            allowed_states = tuple(
                state if isinstance(state, JobState) else JobState(state)
                for state in allowed_predecessor_states
            )
        except (TypeError, ValueError) as exc:
            raise ChainInputError("allowed_predecessor_states must contain canonical states") from exc
        if not allowed_states or any(state not in TERMINAL_STATES for state in allowed_states):
            raise ChainInputError("chain predecessors must be gated by one or more terminal states")
        if isinstance(parent_artifact_ids, str) or not isinstance(parent_artifact_ids, (tuple, list)):
            raise ChainInputError("parent_artifact_ids must be a list of strings")
        artifact_ids = tuple(parent_artifact_ids)
        if any(not isinstance(item, str) or not item.strip() for item in artifact_ids):
            raise ChainInputError("parent_artifact_ids must contain non-empty strings")
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ChainInputError("parent_artifact_ids must not contain duplicates")
        if isinstance(parent_messages, str) or not isinstance(parent_messages, (tuple, list)):
            raise ChainInputError("parent_messages must be a list of strings")
        messages = tuple(parent_messages)
        if len(messages) > 16 or any(
            not isinstance(item, str) or not item.strip() or len(item) > 2_000 for item in messages
        ):
            raise ChainInputError("parent_messages must contain at most 16 non-empty strings of at most 2000 characters")
        key = idempotency_key or f"chain:{chain_id}:step:{step_id}"
        request_sha256 = self._chain_request_sha256(
            chain_id=chain_id,
            step_id=step_id,
            step_index=step_index,
            task=task,
            predecessor_task_id=predecessor_task_id,
            allowed_states=allowed_states,
            parent_artifact_ids=artifact_ids,
            parent_messages=messages,
            idempotency_key=key,
            requested_by=requested_by,
            correlation_id=correlation_id,
            workspace_policy=workspace_policy,
            priority=priority,
            deadline_at=deadline_at,
        )
        policy = {"linear": True}

        with self._connection(write=True) as connection:
            existing_step = connection.execute(
                "SELECT * FROM chain_steps WHERE chain_id = ? AND step_id = ?",
                (chain_id, step_id),
            ).fetchone()
            if existing_step is not None:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE task_id = ?", (existing_step["task_id"],)
                ).fetchone()
                if row is None:
                    raise StoreError("chain step references a missing job")
                existing = self._row_envelope(row)
                if (
                    existing.task_id != task.task_id
                    or existing.idempotency_key != key
                    or (existing_step["request_sha256"] and existing_step["request_sha256"] != request_sha256)
                ):
                    raise IdempotencyConflict(f"chain step {chain_id}/{step_id} already exists")
                return existing, False

            conflicting_job = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ? OR task_id = ?",
                (key, task.task_id),
            ).fetchone()
            if conflicting_job is not None:
                raise IdempotencyConflict(
                    f"chain step conflicts with existing task {conflicting_job['task_id']}"
                )

            chain_row = connection.execute(
                "SELECT * FROM chains WHERE chain_id = ?", (chain_id,)
            ).fetchone()
            if chain_row is None:
                if step_index != 0 or predecessor_task_id is not None:
                    raise ChainConflict("the first chain step must have index 0 and no predecessor")
                connection.execute(
                    "INSERT INTO chains (chain_id, created_at, updated_at, policy_json) VALUES (?, ?, ?, ?)",
                    (chain_id, utc_now(), utc_now(), self._dump(policy)),
                )
            else:
                last = connection.execute(
                    "SELECT MAX(step_index) AS last_index FROM chain_steps WHERE chain_id = ?",
                    (chain_id,),
                ).fetchone()["last_index"]
                if step_index != int(last) + 1:
                    raise ChainConflict(
                        f"chain {chain_id} requires step index {int(last) + 1}, got {step_index}"
                    )
                stored_policy = self._load(chain_row["policy_json"])
                if stored_policy != policy:
                    raise ChainConflict("chain policy is incompatible with this step")

            parent_artifacts: tuple[ArtifactRef, ...] = ()
            if step_index == 0:
                if artifact_ids or messages:
                    raise ChainInputError("the first chain step cannot declare parent inputs")
            else:
                if predecessor_task_id is None:
                    raise ChainConflict("non-first chain steps require a predecessor_task_id")
                predecessor_step = connection.execute(
                    "SELECT task_id FROM chain_steps WHERE chain_id = ? AND step_index = ?",
                    (chain_id, step_index - 1),
                ).fetchone()
                if predecessor_step is None or predecessor_step["task_id"] != predecessor_task_id:
                    raise ChainConflict("predecessor_task_id must identify the immediately preceding chain step")
                predecessor_row = connection.execute(
                    "SELECT * FROM jobs WHERE task_id = ?", (predecessor_task_id,)
                ).fetchone()
                if predecessor_row is None:
                    raise JobNotFound(predecessor_task_id)
                predecessor = self._row_envelope(predecessor_row)
                if predecessor.state not in allowed_states:
                    allowed_text = ", ".join(state.value for state in allowed_states)
                    raise ChainNotReady(
                        f"predecessor {predecessor_task_id} is {predecessor.state.value}; allowed states: {allowed_text}"
                    )
                if artifact_ids:
                    placeholders = ",".join("?" for _ in artifact_ids)
                    rows = connection.execute(
                        f"SELECT * FROM artifacts WHERE task_id = ? AND artifact_id IN ({placeholders})",
                        (predecessor_task_id, *artifact_ids),
                    ).fetchall()
                    by_id = {row["artifact_id"]: self._artifact_ref(row) for row in rows}
                    missing = [item for item in artifact_ids if item not in by_id]
                    if missing:
                        raise ChainInputError(
                            "parent artifact is not declared on the predecessor: " + ", ".join(missing)
                        )
                    parent_artifacts = tuple(by_id[item] for item in artifact_ids)

            envelope = JobEnvelope.new(
                task,
                idempotency_key=key,
                requested_by=requested_by,
                correlation_id=correlation_id,
                workspace_policy=workspace_policy,
                priority=priority,
                deadline_at=deadline_at,
                chain_id=chain_id,
                chain_step_id=step_id,
                chain_step_index=step_index,
                predecessor_task_id=predecessor_task_id,
                parent_artifacts=parent_artifacts,
                parent_messages=messages,
            )
            self._persist(connection, envelope)
            now = utc_now()
            connection.execute(
                "INSERT INTO chain_steps (chain_id, step_id, step_index, task_id, predecessor_task_id, idempotency_key, request_sha256, allowed_states_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chain_id,
                    step_id,
                    step_index,
                    task.task_id,
                    predecessor_task_id,
                    key,
                    request_sha256,
                    self._dump({"states": [state.value for state in allowed_states]}),
                    now,
                ),
            )
            connection.execute(
                "UPDATE chains SET updated_at = ? WHERE chain_id = ?", (now, chain_id)
            )
            return envelope, True

    def schedule_chain_step(
        self,
        *,
        chain_id: str,
        step_id: str,
        step_index: int,
        task: Any,
        predecessor_task_id: str | None = None,
        allowed_predecessor_states: tuple[JobState | str, ...] = (JobState.SUCCEEDED,),
        parent_artifact_ids: tuple[str, ...] = (),
        parent_messages: tuple[str, ...] = (),
        idempotency_key: str | None = None,
        requested_by: str = "orchestrator",
        correlation_id: str | None = None,
        workspace_policy: Mapping[str, Any] | None = None,
        priority: int = 0,
        deadline_at: str | None = None,
    ) -> ChainSchedule:
        """Schedule a child before its predecessor is terminal.

        A ready predecessor is submitted immediately through the existing
        idempotent path. A live predecessor creates a durable pending recipe;
        terminal completion or the final parent artifact automatically
        materializes that recipe in the same SQLite transaction. A terminal
        predecessor in a disallowed state is rejected rather than left in an
        ambiguous pending state.
        """

        try:
            envelope, created = self.submit_chain_step(
                chain_id=chain_id,
                step_id=step_id,
                step_index=step_index,
                task=task,
                predecessor_task_id=predecessor_task_id,
                allowed_predecessor_states=allowed_predecessor_states,
                parent_artifact_ids=parent_artifact_ids,
                parent_messages=parent_messages,
                idempotency_key=idempotency_key,
                requested_by=requested_by,
                correlation_id=correlation_id,
                workspace_policy=workspace_policy,
                priority=priority,
                deadline_at=deadline_at,
            )
            return ChainSchedule(
                chain_id=chain_id,
                step_id=step_id,
                step_index=step_index,
                created=created,
                pending=False,
                envelope=envelope,
            )
        except ChainNotReady:
            if predecessor_task_id is None:
                raise
            predecessor = self.get(predecessor_task_id)
            if predecessor.state in TERMINAL_STATES:
                raise

        if not isinstance(task, DelegationTask):
            raise ChainInputError("task must be a DelegationTask")
        try:
            allowed_states = tuple(
                state if isinstance(state, JobState) else JobState(state)
                for state in allowed_predecessor_states
            )
        except (TypeError, ValueError) as exc:
            raise ChainInputError("allowed_predecessor_states must contain canonical states") from exc
        key = idempotency_key or f"chain:{chain_id}:step:{step_id}"
        request_sha256 = self._chain_request_sha256(
            chain_id=chain_id,
            step_id=step_id,
            step_index=step_index,
            task=task,
            predecessor_task_id=predecessor_task_id,
            allowed_states=allowed_states,
            parent_artifact_ids=tuple(parent_artifact_ids),
            parent_messages=tuple(parent_messages),
            idempotency_key=key,
            requested_by=requested_by,
            correlation_id=correlation_id,
            workspace_policy=workspace_policy,
            priority=priority,
            deadline_at=deadline_at,
        )
        request = {
            "task": task.to_dict(),
            "predecessor_task_id": predecessor_task_id,
            "allowed_predecessor_states": [state.value for state in allowed_states],
            "parent_artifact_ids": list(parent_artifact_ids),
            "parent_messages": list(parent_messages),
            "idempotency_key": key,
            "requested_by": requested_by,
            "correlation_id": correlation_id,
            "workspace_policy": dict(workspace_policy or {}),
            "priority": priority,
            "deadline_at": deadline_at,
        }
        now = utc_now()
        with self._connection(write=True) as connection:
            existing = connection.execute(
                "SELECT * FROM pending_chain_steps WHERE chain_id = ? AND step_id = ?",
                (chain_id, step_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["task_id"] != task.task_id
                    or existing["idempotency_key"] != key
                    or (existing["request_sha256"] and existing["request_sha256"] != request_sha256)
                ):
                    raise IdempotencyConflict(f"chain step {chain_id}/{step_id} already exists")
                return ChainSchedule(
                    chain_id=chain_id,
                    step_id=step_id,
                    step_index=int(existing["step_index"]),
                    created=False,
                    pending=existing["status"] == "pending",
                    error=existing["last_error"],
                )
            conflict = connection.execute(
                "SELECT task_id FROM pending_chain_steps WHERE idempotency_key = ? OR task_id = ?",
                (key, task.task_id),
            ).fetchone()
            if conflict is not None:
                raise IdempotencyConflict(f"pending chain step conflicts with existing task {conflict['task_id']}")
            if connection.execute(
                "SELECT 1 FROM jobs WHERE idempotency_key = ? OR task_id = ?",
                (key, task.task_id),
            ).fetchone() is not None:
                raise IdempotencyConflict(f"chain step conflicts with existing task {task.task_id}")
            chain = connection.execute(
                "SELECT * FROM chains WHERE chain_id = ?", (chain_id,)
            ).fetchone()
            if chain is None:
                raise ChainConflict("a deferred step requires an existing chain root")
            last = connection.execute(
                "SELECT MAX(step_index) AS last_index FROM chain_steps WHERE chain_id = ?",
                (chain_id,),
            ).fetchone()["last_index"]
            if last is None or step_index != int(last) + 1:
                raise ChainConflict(
                    f"chain {chain_id} requires step index {int(last or -1) + 1}, got {step_index}"
                )
            previous = connection.execute(
                "SELECT task_id FROM chain_steps WHERE chain_id = ? AND step_index = ?",
                (chain_id, step_index - 1),
            ).fetchone()
            if previous is None or previous["task_id"] != predecessor_task_id:
                raise ChainConflict("predecessor_task_id must identify the immediately preceding chain step")
            connection.execute(
                "INSERT INTO pending_chain_steps (chain_id, step_id, step_index, task_id, predecessor_task_id, idempotency_key, request_sha256, request_json, allowed_states_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    chain_id,
                    step_id,
                    step_index,
                    task.task_id,
                    predecessor_task_id,
                    key,
                    request_sha256,
                    self._dump(request),
                    self._dump({"states": [state.value for state in allowed_states]}),
                    now,
                    now,
                ),
            )
            connection.execute("UPDATE chains SET updated_at = ? WHERE chain_id = ?", (now, chain_id))
            # Close the race where the predecessor becomes terminal between
            # the initial readiness check and this durable pending insert.
            self._activate_pending_chain_steps(
                connection,
                predecessor_task_id=predecessor_task_id,
            )
            resolved = connection.execute(
                "SELECT * FROM pending_chain_steps WHERE chain_id = ? AND step_id = ?",
                (chain_id, step_id),
            ).fetchone()
            if resolved["status"] == "materialized":
                materialized = connection.execute(
                    "SELECT * FROM jobs WHERE task_id = ?",
                    (resolved["materialized_task_id"],),
                ).fetchone()
                return ChainSchedule(
                    chain_id=chain_id,
                    step_id=step_id,
                    step_index=step_index,
                    created=True,
                    pending=False,
                    envelope=self._row_envelope(materialized),
                )
            if resolved["status"] == "blocked":
                raise ChainNotReady(resolved["last_error"] or "deferred chain step was blocked")
            if resolved["last_error"]:
                return ChainSchedule(
                    chain_id=chain_id,
                    step_id=step_id,
                    step_index=step_index,
                    created=True,
                    pending=True,
                    error=resolved["last_error"],
                )
        return ChainSchedule(
            chain_id=chain_id,
            step_id=step_id,
            step_index=step_index,
            created=True,
            pending=True,
        )

    def _activate_pending_chain_steps(
        self,
        connection: sqlite3.Connection,
        *,
        predecessor_task_id: str,
    ) -> None:
        """Materialize pending children unlocked by one terminal parent."""

        rows = connection.execute(
            "SELECT * FROM pending_chain_steps WHERE predecessor_task_id = ? AND status = 'pending' ORDER BY step_index",
            (predecessor_task_id,),
        ).fetchall()
        if not rows:
            return
        predecessor_row = connection.execute(
            "SELECT * FROM jobs WHERE task_id = ?", (predecessor_task_id,)
        ).fetchone()
        if predecessor_row is None:
            return
        predecessor = self._row_envelope(predecessor_row)
        for row in rows:
            allowed = self._load(row["allowed_states_json"]).get("states", [])
            if predecessor.state.value not in allowed:
                if predecessor.state not in TERMINAL_STATES:
                    # The recipe was intentionally registered before the
                    # predecessor completed; leave it pending until a
                    # terminal transition or reconciliation pass.
                    continue
                now = utc_now()
                connection.execute(
                    "UPDATE pending_chain_steps SET status = 'blocked', last_error = ?, updated_at = ?, resolved_at = ? WHERE chain_id = ? AND step_id = ? AND status = 'pending'",
                    (
                        f"predecessor {predecessor_task_id} reached disallowed state {predecessor.state.value}",
                        now,
                        now,
                        row["chain_id"],
                        row["step_id"],
                    ),
                )
                continue
            try:
                request = self._load(row["request_json"])
                artifact_ids = tuple(request.get("parent_artifact_ids", ()))
                parent_artifacts: tuple[ArtifactRef, ...] = ()
                if artifact_ids:
                    placeholders = ",".join("?" for _ in artifact_ids)
                    artifact_rows = connection.execute(
                        f"SELECT * FROM artifacts WHERE task_id = ? AND artifact_id IN ({placeholders})",
                        (predecessor_task_id, *artifact_ids),
                    ).fetchall()
                    by_id = {item["artifact_id"]: self._artifact_ref(item) for item in artifact_rows}
                    missing = [item for item in artifact_ids if item not in by_id]
                    if missing:
                        now = utc_now()
                        connection.execute(
                            "UPDATE pending_chain_steps SET last_error = ?, updated_at = ? WHERE chain_id = ? AND step_id = ? AND status = 'pending'",
                            ("parent artifacts not available: " + ", ".join(missing), now, row["chain_id"], row["step_id"]),
                        )
                        continue
                    parent_artifacts = tuple(by_id[item] for item in artifact_ids)
                task = DelegationTask.from_dict(request.get("task", {}))
                conflict = connection.execute(
                    "SELECT task_id FROM jobs WHERE idempotency_key = ? OR task_id = ?",
                    (row["idempotency_key"], task.task_id),
                ).fetchone()
                if conflict is not None:
                    now = utc_now()
                    connection.execute(
                        "UPDATE pending_chain_steps SET status = 'blocked', last_error = ?, updated_at = ?, resolved_at = ? WHERE chain_id = ? AND step_id = ? AND status = 'pending'",
                        (f"materialization conflicts with existing task {conflict['task_id']}", now, now, row["chain_id"], row["step_id"]),
                    )
                    continue
                connection.execute("SAVEPOINT materialize_chain_step")
                try:
                    envelope = JobEnvelope.new(
                        task,
                        idempotency_key=row["idempotency_key"],
                        requested_by=request.get("requested_by", "orchestrator"),
                        correlation_id=request.get("correlation_id"),
                        workspace_policy=request.get("workspace_policy", {}),
                        priority=int(request.get("priority", 0)),
                        deadline_at=request.get("deadline_at"),
                        chain_id=row["chain_id"],
                        chain_step_id=row["step_id"],
                        chain_step_index=int(row["step_index"]),
                        predecessor_task_id=predecessor_task_id,
                        parent_artifacts=parent_artifacts,
                        parent_messages=tuple(request.get("parent_messages", ())),
                    )
                    self._persist(connection, envelope)
                    now = utc_now()
                    connection.execute(
                        "INSERT INTO chain_steps (chain_id, step_id, step_index, task_id, predecessor_task_id, idempotency_key, request_sha256, allowed_states_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            row["chain_id"],
                            row["step_id"],
                            row["step_index"],
                            task.task_id,
                            predecessor_task_id,
                            row["idempotency_key"],
                            row["request_sha256"],
                            row["allowed_states_json"],
                            row["created_at"],
                        ),
                    )
                    connection.execute("UPDATE chains SET updated_at = ? WHERE chain_id = ?", (now, row["chain_id"]))
                    connection.execute(
                        "UPDATE pending_chain_steps SET status = 'materialized', materialized_task_id = ?, last_error = NULL, updated_at = ?, resolved_at = ? WHERE chain_id = ? AND step_id = ? AND status = 'pending'",
                        (task.task_id, now, now, row["chain_id"], row["step_id"]),
                    )
                except Exception:
                    connection.execute("ROLLBACK TO materialize_chain_step")
                    connection.execute("RELEASE materialize_chain_step")
                    raise
                else:
                    connection.execute("RELEASE materialize_chain_step")
            except Exception as exc:
                now = utc_now()
                connection.execute(
                    "UPDATE pending_chain_steps SET status = 'blocked', last_error = ?, updated_at = ?, resolved_at = ? WHERE chain_id = ? AND step_id = ? AND status = 'pending'",
                    (f"materialization error: {type(exc).__name__}: {exc}"[:2000], now, now, row["chain_id"], row["step_id"]),
                )

    def get_chain(self, chain_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            chain = connection.execute(
                "SELECT * FROM chains WHERE chain_id = ?", (chain_id,)
            ).fetchone()
            if chain is None:
                raise JobNotFound(f"chain {chain_id} not found")
            rows = connection.execute(
                "SELECT cs.*, j.* FROM chain_steps cs JOIN jobs j ON j.task_id = cs.task_id WHERE cs.chain_id = ? ORDER BY cs.step_index",
                (chain_id,),
            ).fetchall()
            steps = []
            for row in rows:
                envelope = self._row_envelope(row)
                steps.append(
                    {
                        "chain_id": chain_id,
                        "step_id": row["step_id"],
                        "step_index": row["step_index"],
                        "task_id": row["task_id"],
                        "predecessor_task_id": row["predecessor_task_id"],
                        "idempotency_key": row["idempotency_key"],
                        "allowed_predecessor_states": self._load(row["allowed_states_json"]).get("states", []),
                        "envelope": envelope.to_dict(),
                    }
                )
            pending_rows = connection.execute(
                "SELECT * FROM pending_chain_steps WHERE chain_id = ? ORDER BY step_index",
                (chain_id,),
            ).fetchall()
            pending_steps = [
                {
                    "chain_id": row["chain_id"],
                    "step_id": row["step_id"],
                    "step_index": row["step_index"],
                    "task_id": row["task_id"],
                    "predecessor_task_id": row["predecessor_task_id"],
                    "idempotency_key": row["idempotency_key"],
                    "allowed_predecessor_states": self._load(row["allowed_states_json"]).get("states", []),
                    "status": row["status"],
                    "materialized_task_id": row["materialized_task_id"],
                    "last_error": row["last_error"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "resolved_at": row["resolved_at"],
                }
                for row in pending_rows
            ]
            return {
                "chain_id": chain_id,
                "created_at": chain["created_at"],
                "updated_at": chain["updated_at"],
                "policy": self._load(chain["policy_json"]),
                "steps": steps,
                "pending_steps": pending_steps,
            }

    def reconcile_pending_chains(self, *, chain_id: str | None = None) -> dict[str, int | str | None]:
        """Replay deferred chain activation after coordinator restart or repair."""

        with self._connection(write=True) as connection:
            if chain_id is not None and connection.execute(
                "SELECT 1 FROM chains WHERE chain_id = ?", (chain_id,)
            ).fetchone() is None:
                raise JobNotFound(f"chain {chain_id} not found")
            query = "SELECT DISTINCT predecessor_task_id FROM pending_chain_steps WHERE status = 'pending'"
            params: tuple[Any, ...] = ()
            if chain_id is not None:
                query += " AND chain_id = ?"
                params = (chain_id,)
            predecessors = connection.execute(query, params).fetchall()
            for row in predecessors:
                self._activate_pending_chain_steps(
                    connection,
                    predecessor_task_id=row["predecessor_task_id"],
                )
            pending_query = "SELECT COUNT(*) AS count FROM pending_chain_steps WHERE status = 'pending'"
            blocked_query = "SELECT COUNT(*) AS count FROM pending_chain_steps WHERE status = 'blocked'"
            materialized_query = "SELECT COUNT(*) AS count FROM pending_chain_steps WHERE status = 'materialized'"
            count_params: tuple[Any, ...] = ()
            if chain_id is not None:
                suffix = " AND chain_id = ?"
                pending_query += suffix
                blocked_query += suffix
                materialized_query += suffix
                count_params = (chain_id,)
            return {
                "chain_id": chain_id,
                "pending": int(connection.execute(pending_query, count_params).fetchone()["count"]),
                "blocked": int(connection.execute(blocked_query, count_params).fetchone()["count"]),
                "materialized": int(connection.execute(materialized_query, count_params).fetchone()["count"]),
            }

    def get(self, task_id: str) -> JobEnvelope:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise JobNotFound(task_id)
            return self._row_envelope(row)

    def _get_with_version(self, connection: sqlite3.Connection, task_id: str) -> tuple[JobEnvelope, int]:
        row = connection.execute("SELECT * FROM jobs WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise JobNotFound(task_id)
        return self._row_envelope(row), int(row["version"])

    def list_jobs(self, *, state: JobState | str | None = None) -> list[JobEnvelope]:
        with self._connection() as connection:
            if state is None:
                rows = connection.execute("SELECT * FROM jobs").fetchall()
            else:
                selected = state.value if isinstance(state, JobState) else JobState(state).value
                rows = connection.execute(
                    "SELECT * FROM jobs WHERE state = ?",
                    (selected,),
                ).fetchall()
            envelopes = [self._row_envelope(row) for row in rows]
            return sorted(
                envelopes,
                key=lambda item: (
                    -item.priority,
                    _parse_time(item.deadline_at).timestamp() if item.deadline_at else float("inf"),
                    item.created_at,
                    item.task_id,
                ),
            )

    def expire_overdue_jobs(self) -> int:
        """Expire overdue, unleased work without interrupting active execution."""

        now = datetime.now(timezone.utc)
        expired_count = 0
        with self._connection(write=True) as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE state IN (?, ?, ?)",
                (JobState.SUBMITTED.value, JobState.ACCEPTED.value, JobState.WAITING.value),
            ).fetchall()
            for row in rows:
                envelope = self._row_envelope(row)
                if envelope.deadline_at is None or _parse_time(envelope.deadline_at) > now:
                    continue
                lease = connection.execute(
                    "SELECT * FROM leases WHERE task_id = ?", (envelope.task_id,)
                ).fetchone()
                if lease is not None and not _expired(lease["expires_at"], now=now):
                    continue
                if lease is not None:
                    connection.execute("DELETE FROM leases WHERE task_id = ?", (envelope.task_id,))
                receipt = JobReceipt(
                    receipt_id=f"receipt_{envelope.task_id}_expired_{int(now.timestamp() * 1000)}",
                    task_id=envelope.task_id,
                    final_state=JobState.EXPIRED,
                    actor="coordinator",
                    completed_at=utc_now(),
                    evidence={
                        "deadline_at": envelope.deadline_at,
                        "execution_started": False,
                        "expired_by": "coordinator_scheduler",
                    },
                    summary="Task expired before a worker began execution.",
                )
                updated = envelope.transition(
                    JobState.EXPIRED,
                    actor="coordinator",
                    reason="task deadline elapsed before execution",
                    evidence=dict(receipt.evidence),
                    receipt=receipt,
                )
                updated = replace(updated, lease_id=None, lease_expires_at=None)
                self._persist(connection, updated, expected_version=int(row["version"]))
                expired_count += 1
        return expired_count

    def events_since(self, task_id: str, sequence: int = 0) -> list[MessageUpdate]:
        self.get(task_id)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT event_json FROM events WHERE task_id = ? AND sequence > ? ORDER BY sequence",
                (task_id, sequence),
            ).fetchall()
            return [MessageUpdate.from_dict(self._load(row["event_json"])) for row in rows]

    def transition(
        self,
        task_id: str,
        target: JobState | str,
        *,
        actor: str,
        reason: str,
        evidence: Mapping[str, Any] | None = None,
        receipt: JobReceipt | None = None,
        lease_id: str | None = None,
        progress: float | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> JobEnvelope:
        with self._connection(write=True) as connection:
            envelope, version = self._get_with_version(connection, task_id)
            self._check_lease(connection, envelope, lease_id=lease_id, actor=actor)
            if receipt is not None:
                self._validate_receipt_artifacts(connection, task_id, receipt)
            updated = envelope.transition(
                target,
                actor=actor,
                reason=reason,
                evidence=evidence,
                receipt=receipt,
                progress=progress,
                data=data,
            )
            if updated.state in TERMINAL_STATES:
                updated = replace(updated, lease_id=None, lease_expires_at=None)
                connection.execute("DELETE FROM leases WHERE task_id = ?", (task_id,))
            self._persist(connection, updated, expected_version=version)
            if updated.state in TERMINAL_STATES:
                self._activate_pending_chain_steps(
                    connection,
                    predecessor_task_id=task_id,
                )
            return updated

    @staticmethod
    def _validate_receipt_artifacts(
        connection: sqlite3.Connection,
        task_id: str,
        receipt: JobReceipt,
    ) -> None:
        if receipt.task_id != task_id:
            raise StoreError("receipt task_id does not match the transitioned task")
        seen: set[str] = set()
        for artifact in receipt.artifacts:
            if artifact.artifact_id in seen:
                raise StoreError(f"receipt repeats artifact {artifact.artifact_id}")
            seen.add(artifact.artifact_id)
            row = connection.execute(
                "SELECT * FROM artifacts WHERE task_id = ? AND artifact_id = ?",
                (task_id, artifact.artifact_id),
            ).fetchone()
            if row is None:
                raise StoreError(f"receipt references missing artifact {artifact.artifact_id}")
            stored = RelayStore._artifact_ref(row)
            if stored.to_dict() != artifact.to_dict():
                raise StoreError(f"receipt artifact {artifact.artifact_id} does not match stored metadata")
            content = bytes(row["content"])
            if hashlib.sha256(content).hexdigest() != stored.sha256 or len(content) != stored.size_bytes:
                raise StoreError(f"stored artifact {artifact.artifact_id} failed hash verification")

    def record_update(
        self,
        task_id: str,
        *,
        actor: str,
        reason: str,
        lease_id: str | None = None,
        progress: float | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> JobEnvelope:
        with self._connection(write=True) as connection:
            envelope, version = self._get_with_version(connection, task_id)
            if envelope.state in TERMINAL_STATES:
                raise StoreError(f"cannot update terminal task {task_id}")
            self._check_lease(connection, envelope, lease_id=lease_id, actor=actor)
            updated = envelope._append_event(
                state=envelope.state,
                actor=actor,
                reason=reason,
                timestamp=utc_now(),
                progress=progress,
                data=data,
            )
            self._persist(connection, updated, expected_version=version)
            return updated

    @staticmethod
    def _check_lease(
        connection: sqlite3.Connection,
        envelope: JobEnvelope,
        *,
        lease_id: str | None,
        actor: str,
    ) -> None:
        row = connection.execute("SELECT * FROM leases WHERE task_id = ?", (envelope.task_id,)).fetchone()
        if row is None:
            if actor not in {"coordinator", "client"}:
                raise LeaseNotFound(f"no active lease for {envelope.task_id}")
            return
        if _expired(row["expires_at"]):
            raise LeaseConflict(f"lease {row['lease_id']} for {envelope.task_id} has expired")
        if actor not in {"coordinator", "client"} and lease_id != row["lease_id"]:
            raise LeaseConflict(f"worker {actor} does not own the active lease for {envelope.task_id}")

    def acquire_lease(
        self,
        task_id: str,
        *,
        worker_id: str,
        ttl_seconds: float = 60,
        actor: str = "coordinator",
    ) -> tuple[JobEnvelope, LeaseGrant]:
        expires_at = _expires_after(ttl_seconds)
        with self._connection(write=True) as connection:
            envelope, version = self._get_with_version(connection, task_id)
            if envelope.state in TERMINAL_STATES:
                raise LeaseConflict(f"cannot lease terminal task {task_id}")
            row = connection.execute("SELECT * FROM leases WHERE task_id = ?", (task_id,)).fetchone()
            if row is not None and not _expired(row["expires_at"]):
                if row["worker_id"] == worker_id:
                    return envelope, LeaseGrant(
                        task_id=task_id,
                        lease_id=row["lease_id"],
                        worker_id=worker_id,
                        expires_at=row["expires_at"],
                        renewed=False,
                    )
                raise LeaseConflict(
                    f"task {task_id} is leased by worker {row['worker_id']} until {row['expires_at']}"
                )
            if row is not None:
                connection.execute("DELETE FROM leases WHERE task_id = ?", (task_id,))
                if envelope.state is JobState.CANCEL_REQUESTED:
                    receipt = JobReceipt(
                        receipt_id=f"receipt_{task_id}_cancel-expired_{int(datetime.now(timezone.utc).timestamp() * 1000)}",
                        task_id=task_id,
                        final_state=JobState.BLOCKED,
                        actor=actor,
                        completed_at=utc_now(),
                        evidence={
                            "cancel_requested": True,
                            "execution_stopped": False,
                            "recovery": "lease expired before the worker confirmed cancellation",
                        },
                        summary="Cancellation could not be confirmed before the worker lease expired.",
                    )
                    envelope = envelope.transition(
                        JobState.BLOCKED,
                        actor=actor,
                        reason="cancel requested but worker lease expired before stop confirmation",
                        evidence=dict(receipt.evidence),
                        receipt=receipt,
                    )
                    envelope = replace(envelope, lease_id=None, lease_expires_at=None, worker_id=None)
                    self._persist(connection, envelope, expected_version=version)
                    # Persist the terminal safety decision before surfacing a
                    # lease conflict to the worker that attempted recovery.
                    connection.commit()
                    raise LeaseConflict(
                        f"task {task_id} was blocked because cancellation was not confirmed before lease expiry"
                    )
                if envelope.state is JobState.RUNNING:
                    envelope = envelope.transition(
                        JobState.WAITING,
                        actor=actor,
                        reason="worker lease expired; task returned to waiting",
                        data={"previous_lease_id": row["lease_id"], "previous_worker_id": row["worker_id"]},
                    )
                    version = self._persist(connection, envelope, expected_version=version)
                else:
                    envelope = envelope._append_event(
                        state=envelope.state,
                        actor=actor,
                        reason="worker lease expired; ownership released",
                        timestamp=utc_now(),
                        data={"previous_lease_id": row["lease_id"], "previous_worker_id": row["worker_id"]},
                    )
                    version = self._persist(connection, envelope, expected_version=version)
            if envelope.state in {JobState.SUBMITTED, JobState.WAITING}:
                envelope = envelope.transition(
                    JobState.ACCEPTED,
                    actor=actor,
                    reason="worker lease accepted",
                )
                version = self._persist(connection, envelope, expected_version=version)
            lease_id = f"lease_{task_id}_{worker_id}_{int(datetime.now(timezone.utc).timestamp() * 1000000)}"
            envelope = envelope.assign_lease(
                worker_id=worker_id,
                lease_id=lease_id,
                lease_expires_at=expires_at,
                actor=actor,
            )
            connection.execute(
                "INSERT INTO leases (task_id, lease_id, worker_id, expires_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, lease_id, worker_id, expires_at, utc_now()),
            )
            self._persist(connection, envelope, expected_version=version)
            return envelope, LeaseGrant(task_id, lease_id, worker_id, expires_at)

    def claim_next(
        self,
        *,
        worker_id: str,
        ttl_seconds: float = 60,
    ) -> tuple[JobEnvelope, LeaseGrant] | None:
        """Return and lease the highest-priority compatible task for a worker.

        Selection is deliberately coordinator-owned, but the existing
        ``acquire_lease`` transaction remains the final ownership boundary.
        If another worker wins a candidate between selection and acquisition,
        this method skips it and continues through the queue.  Incompatible
        task policies are also skipped so one specialized task cannot block
        unrelated work behind it.
        """

        # A worker must have an enrolled card before it can participate in
        # automatic scheduling.  The HTTP layer authenticates scoped workers;
        # this check also protects admin callers from silently scheduling an
        # unknown identity.
        card = self.get_agent(worker_id)
        if card.readiness is Readiness.BLOCKED or card.metadata.get("revoked") is True:
            raise AgentCapabilityError(f"agent {worker_id} is blocked or revoked")

        self.expire_overdue_jobs()
        candidates = self.list_jobs()
        for envelope in candidates:
            if envelope.state not in {
                JobState.SUBMITTED,
                JobState.ACCEPTED,
                JobState.WAITING,
                JobState.RUNNING,
            }:
                continue
            # ``acquire_lease`` is intentionally idempotent for the current
            # owner, but a queue claim must not hand the same live work back
            # to a worker that is already executing it.
            if (
                envelope.lease_id
                and envelope.lease_expires_at
                and not _expired(envelope.lease_expires_at)
            ):
                continue
            try:
                self.assert_agent_can_claim(envelope.task_id, worker_id=worker_id)
            except AgentCapabilityError:
                continue
            try:
                return self.acquire_lease(
                    envelope.task_id,
                    worker_id=worker_id,
                    ttl_seconds=ttl_seconds,
                    actor=worker_id,
                )
            except LeaseConflict:
                # A live lease belongs to another worker, or cancellation
                # safety converted an expired task to a terminal state.
                continue
        return None

    def assert_agent_can_access_task(self, task_id: str, *, worker_id: str) -> None:
        """Require a scoped worker to own the task's active lease."""

        with self._connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise JobNotFound(task_id)
            envelope = self._row_envelope(row)
            if envelope.state in TERMINAL_STATES and envelope.worker_id == worker_id:
                return
            lease = connection.execute(
                "SELECT worker_id, expires_at FROM leases WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if lease is None or lease["worker_id"] != worker_id or _expired(lease["expires_at"]):
                raise AgentAccessError(f"agent {worker_id} does not own task {task_id}")

    def assert_agent_owns_lease(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_id: str | None = None,
    ) -> None:
        with self._connection() as connection:
            lease = connection.execute(
                "SELECT worker_id, lease_id, expires_at FROM leases WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if (
                lease is None
                or lease["worker_id"] != worker_id
                or (lease_id is not None and lease["lease_id"] != lease_id)
                or _expired(lease["expires_at"])
            ):
                raise AgentAccessError(f"agent {worker_id} does not own an active lease for {task_id}")

    def list_jobs_for_agent(self, worker_id: str) -> list[JobEnvelope]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT j.*, l.worker_id AS lease_worker_id, l.expires_at FROM jobs j LEFT JOIN leases l ON l.task_id = j.task_id ORDER BY j.created_at, j.task_id",
            ).fetchall()
            result: list[JobEnvelope] = []
            for row in rows:
                envelope = self._row_envelope(row)
                active = (
                    row["lease_worker_id"] == worker_id
                    and row["expires_at"] is not None
                    and not _expired(row["expires_at"])
                )
                historical = envelope.state in TERMINAL_STATES and envelope.worker_id == worker_id
                if active or historical:
                    result.append(envelope)
            return result

    def assert_agent_can_access_chain(self, chain_id: str, *, worker_id: str) -> None:
        with self._connection() as connection:
            if connection.execute("SELECT 1 FROM chains WHERE chain_id = ?", (chain_id,)).fetchone() is None:
                raise JobNotFound(f"chain {chain_id} not found")
            rows = connection.execute(
                "SELECT j.*, l.worker_id AS lease_worker_id, l.expires_at FROM chain_steps cs JOIN jobs j ON j.task_id = cs.task_id LEFT JOIN leases l ON l.task_id = cs.task_id WHERE cs.chain_id = ?",
                (chain_id,),
            ).fetchall()
            if not any(
                (
                    row["lease_worker_id"] == worker_id
                    and row["expires_at"] is not None
                    and not _expired(row["expires_at"])
                )
                or (
                    self._row_envelope(row).state in TERMINAL_STATES
                    and self._row_envelope(row).worker_id == worker_id
                )
                for row in rows
            ):
                raise AgentAccessError(f"agent {worker_id} does not own a task in chain {chain_id}")

    def assert_agent_can_access_artifact(
        self,
        task_id: str,
        artifact_id: str,
        *,
        worker_id: str,
    ) -> None:
        """Allow a worker its leased task or an explicitly granted parent artifact."""

        with self._connection() as connection:
            artifact = connection.execute(
                "SELECT 1 FROM artifacts WHERE task_id = ? AND artifact_id = ?",
                (task_id, artifact_id),
            ).fetchone()
            if artifact is None:
                raise StoreError(f"artifact {artifact_id} not found for task {task_id}")
            own = connection.execute(
                "SELECT expires_at FROM leases WHERE task_id = ? AND worker_id = ?",
                (task_id, worker_id),
            ).fetchone()
            if own is not None and not _expired(own["expires_at"]):
                return
            owner = connection.execute(
                "SELECT payload_json FROM jobs WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if owner is not None:
                envelope = self._row_envelope(owner)
                if envelope.state in TERMINAL_STATES and envelope.worker_id == worker_id:
                    return
            leased_children = connection.execute(
                "SELECT j.payload_json, l.expires_at FROM jobs j JOIN leases l ON l.task_id = j.task_id WHERE l.worker_id = ?",
                (worker_id,),
            ).fetchall()
            for child in leased_children:
                if _expired(child["expires_at"]):
                    continue
                envelope = self._row_envelope(child)
                if any(ref.artifact_id == artifact_id for ref in envelope.parent_artifacts):
                    return
            raise AgentAccessError(f"agent {worker_id} has no grant for artifact {artifact_id}")

    def renew_lease(
        self,
        task_id: str,
        *,
        lease_id: str,
        worker_id: str,
        ttl_seconds: float = 60,
    ) -> LeaseGrant:
        expires_at = _expires_after(ttl_seconds)
        with self._connection(write=True) as connection:
            row = connection.execute("SELECT * FROM leases WHERE task_id = ?", (task_id,)).fetchone()
            if row is None or row["lease_id"] != lease_id or row["worker_id"] != worker_id:
                raise LeaseNotFound(f"lease {lease_id} is not owned by {worker_id}")
            if _expired(row["expires_at"]):
                raise LeaseConflict(f"lease {lease_id} has expired")
            connection.execute(
                "UPDATE leases SET expires_at = ?, updated_at = ? WHERE task_id = ?",
                (expires_at, utc_now(), task_id),
            )
            envelope, version = self._get_with_version(connection, task_id)
            if envelope.lease_id != lease_id:
                raise LeaseConflict(f"job {task_id} does not reference lease {lease_id}")
            updated = replace(envelope, lease_expires_at=expires_at)
            updated = updated._append_event(
                state=updated.state,
                actor=worker_id,
                reason="worker lease renewed",
                timestamp=utc_now(),
                data={"lease_id": lease_id, "expires_at": expires_at},
            )
            self._persist(connection, updated, expected_version=version)
            return LeaseGrant(task_id, lease_id, worker_id, expires_at, renewed=True)

    def release_lease(self, task_id: str, *, lease_id: str, worker_id: str) -> JobEnvelope:
        with self._connection(write=True) as connection:
            envelope, version = self._get_with_version(connection, task_id)
            row = connection.execute("SELECT * FROM leases WHERE task_id = ?", (task_id,)).fetchone()
            if row is None or row["lease_id"] != lease_id or row["worker_id"] != worker_id:
                raise LeaseNotFound(f"lease {lease_id} is not owned by {worker_id}")
            connection.execute("DELETE FROM leases WHERE task_id = ?", (task_id,))
            if envelope.state is JobState.RUNNING:
                # Releasing an active execution must not strand the task in
                # running without an owner. The caller is explicitly giving
                # the coordinator permission to make it claimable again.
                updated = envelope.transition(
                    JobState.WAITING,
                    actor=worker_id,
                    reason="worker released active lease; task returned to waiting",
                    data={"lease_id": lease_id},
                )
            else:
                updated = envelope._append_event(
                    state=envelope.state,
                    actor=worker_id,
                    reason="worker released lease",
                    timestamp=utc_now(),
                    data={"lease_id": lease_id},
                )
            updated = replace(updated, lease_id=None, lease_expires_at=None, worker_id=None)
            self._persist(connection, updated, expected_version=version)
            return updated

    def cancel(self, task_id: str, *, actor: str = "client") -> JobEnvelope:
        envelope = self.get(task_id)
        if envelope.state in TERMINAL_STATES or envelope.state is JobState.CANCEL_REQUESTED:
            return envelope
        if envelope.state in {JobState.SUBMITTED, JobState.ACCEPTED} and envelope.lease_id is None:
            receipt = JobReceipt(
                receipt_id=f"receipt_{task_id}_cancel_{int(datetime.now(timezone.utc).timestamp() * 1000)}",
                task_id=task_id,
                final_state=JobState.CANCELLED,
                actor=actor,
                completed_at=utc_now(),
                evidence={"execution_stopped": True, "execution_started": False},
                summary="Task was cancelled before a worker started execution.",
            )
            return self.transition(
                task_id,
                JobState.CANCELLED,
                actor=actor,
                reason="task cancelled before worker execution",
                evidence=dict(receipt.evidence),
                receipt=receipt,
            )
        return self.transition(
            task_id,
            JobState.CANCEL_REQUESTED,
            actor=actor,
            reason="cancellation requested",
            data={"execution_stopped": False},
        )

    def resume(self, task_id: str, *, actor: str = "client") -> JobEnvelope:
        with self._connection(write=True) as connection:
            envelope, version = self._get_with_version(connection, task_id)
            if envelope.state is not JobState.WAITING:
                raise StoreError(f"only waiting tasks can be resumed; {task_id} is {envelope.state.value}")
            connection.execute("DELETE FROM leases WHERE task_id = ?", (task_id,))
            updated = envelope.transition(
                JobState.ACCEPTED,
                actor=actor,
                reason="task resumed and returned to acceptance",
            )
            updated = replace(updated, lease_id=None, lease_expires_at=None)
            self._persist(connection, updated, expected_version=version)
            return updated

    @staticmethod
    def _credential_hash(token: str) -> str:
        if not isinstance(token, str) or not token.strip():
            raise AgentAuthError("agent credential must be a non-empty string")
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def register_agent(self, card: AgentCard, *, credential: str | None = None) -> AgentCard:
        payload = self._dump(card.to_dict())
        with self._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO agents (agent_id, updated_at, card_json) VALUES (?, ?, ?) "
                "ON CONFLICT(agent_id) DO UPDATE SET updated_at = excluded.updated_at, card_json = excluded.card_json",
                (card.agent_id, card.updated_at, payload),
            )
            if credential is not None:
                connection.execute(
                    "INSERT INTO agent_credentials (agent_id, token_hash, created_at, revoked_at) VALUES (?, ?, ?, NULL) "
                    "ON CONFLICT(agent_id) DO UPDATE SET token_hash = excluded.token_hash, created_at = excluded.created_at, revoked_at = NULL",
                    (card.agent_id, self._credential_hash(credential), utc_now()),
                )
        return card

    def authenticate_agent(self, agent_id: str, credential: str | None) -> bool:
        if not isinstance(credential, str) or not credential:
            return False
        with self._connection() as connection:
            row = connection.execute(
                "SELECT token_hash, revoked_at FROM agent_credentials WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
        if row is None or row["revoked_at"] is not None:
            return False
        return hmac.compare_digest(str(row["token_hash"]), self._credential_hash(credential))

    def revoke_agent(self, agent_id: str) -> AgentCard:
        with self._connection(write=True) as connection:
            row = connection.execute("SELECT card_json FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
            if row is None:
                raise StoreError(f"agent {agent_id} not found")
            card = AgentCard.from_dict(self._load(row["card_json"]))
            metadata = dict(card.metadata)
            metadata["revoked"] = True
            updated = replace(card, readiness=Readiness.BLOCKED, metadata=metadata, updated_at=utc_now())
            connection.execute(
                "UPDATE agents SET updated_at = ?, card_json = ? WHERE agent_id = ?",
                (updated.updated_at, self._dump(updated.to_dict()), agent_id),
            )
            connection.execute(
                "UPDATE agent_credentials SET revoked_at = ? WHERE agent_id = ?",
                (utc_now(), agent_id),
            )
            return updated

    def get_agent(self, agent_id: str) -> AgentCard:
        with self._connection() as connection:
            row = connection.execute("SELECT card_json FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
            if row is None:
                raise StoreError(f"agent {agent_id} not found")
            return AgentCard.from_dict(self._load(row["card_json"]))

    def list_agents(
        self,
        *,
        task_kind: str | None = None,
        capability: str | None = None,
        readiness: str | None = None,
    ) -> list[AgentCard]:
        with self._connection() as connection:
            rows = connection.execute("SELECT card_json FROM agents ORDER BY agent_id").fetchall()
            cards = [AgentCard.from_dict(self._load(row["card_json"])) for row in rows]
        if readiness:
            try:
                expected_readiness = readiness if isinstance(readiness, str) else str(readiness)
            except Exception:
                expected_readiness = str(readiness)
            cards = [card for card in cards if card.readiness.value == expected_readiness]
        if task_kind:
            cards = [card for card in cards if task_kind in card.task_kinds]
        if capability:
            cards = [card for card in cards if capability in card.capabilities]
        return cards

    def assert_agent_can_claim(self, task_id: str, *, worker_id: str) -> None:
        """Enforce task routing policy at the coordinator boundary.

        Worker-side filtering is useful for efficiency, but it is not an
        authorization boundary. Scoped workers must also match the enrolled
        Agent Card before the coordinator grants a lease.
        """

        envelope = self.get(task_id)
        card = self.get_agent(worker_id)
        if card.readiness is Readiness.BLOCKED or card.metadata.get("revoked") is True:
            raise AgentCapabilityError(f"agent {worker_id} is blocked or revoked")

        policy = envelope.workspace_policy
        if not isinstance(policy, Mapping):
            raise AgentCapabilityError("workspace_policy must be an object")
        required_backend = policy.get("backend")
        card_backend = card.metadata.get("backend")
        if isinstance(required_backend, str) and required_backend:
            if not isinstance(card_backend, str) or card_backend != required_backend:
                raise AgentCapabilityError(
                    f"agent {worker_id} backend {card_backend!r} does not satisfy {required_backend!r}"
                )

        required = policy.get("required_capabilities", [])
        if isinstance(required, str):
            required = [required]
        if not isinstance(required, (list, tuple)) or any(not isinstance(item, str) or not item for item in required):
            raise AgentCapabilityError("workspace_policy.required_capabilities must be a list of strings")
        missing = sorted(set(required) - set(card.capabilities))
        if missing:
            raise AgentCapabilityError(
                f"agent {worker_id} is missing required capabilities: {', '.join(missing)}"
            )
        if card.task_kinds and envelope.task.task_kind not in card.task_kinds:
            raise AgentCapabilityError(
                f"agent {worker_id} does not advertise task kind {envelope.task.task_kind!r}"
            )

    def heartbeat_agent(
        self,
        agent_id: str,
        *,
        readiness: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AgentCard:
        """Refresh a registered worker without replacing its capability card."""

        card = self.get_agent(agent_id)
        merged_metadata = dict(card.metadata)
        if metadata is not None:
            if not isinstance(metadata, Mapping):
                raise StoreError("agent heartbeat metadata must be an object")
            merged_metadata.update(metadata)
        updated = replace(
            card,
            readiness=readiness if readiness is not None else card.readiness,
            metadata=merged_metadata,
            updated_at=utc_now(),
        )
        return self.register_agent(updated)

    def put_artifact(
        self,
        task_id: str,
        *,
        name: str,
        content: bytes,
        kind: str = "file",
        media_type: str = "application/octet-stream",
        provenance: str = "worker",
        metadata: Mapping[str, Any] | None = None,
        max_bytes: int = 2 * 1024 * 1024,
    ) -> ArtifactRef:
        if not isinstance(content, bytes):
            raise StoreError("artifact content must be bytes")
        if len(content) > max_bytes:
            raise StoreError(f"artifact exceeds {max_bytes} bytes")
        digest = hashlib.sha256(content).hexdigest()
        with self._connection(write=True) as connection:
            if connection.execute("SELECT 1 FROM jobs WHERE task_id = ?", (task_id,)).fetchone() is None:
                raise JobNotFound(task_id)
            existing = connection.execute(
                "SELECT * FROM artifacts WHERE task_id = ? AND sha256 = ? AND name = ?",
                (task_id, digest, name),
            ).fetchone()
            if existing is not None:
                predecessor = self._row_envelope(
                    connection.execute("SELECT * FROM jobs WHERE task_id = ?", (task_id,)).fetchone()
                )
                if predecessor.state in TERMINAL_STATES:
                    self._activate_pending_chain_steps(
                        connection,
                        predecessor_task_id=task_id,
                    )
                return self._artifact_ref(existing)
            artifact_id = f"artifact_{uuid4().hex}"
            uri = f"/tasks/{task_id}/artifacts/{artifact_id}"
            ref = ArtifactRef(
                artifact_id=artifact_id,
                name=name,
                sha256=digest,
                size_bytes=len(content),
                kind=kind,
                media_type=media_type,
                provenance=provenance,
                uri=uri,
                metadata=dict(metadata or {}),
            )
            connection.execute(
                """
                INSERT INTO artifacts
                    (artifact_id, task_id, name, sha256, size_bytes, kind, media_type,
                     provenance, uri, metadata_json, content, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ref.artifact_id,
                    task_id,
                    ref.name,
                    ref.sha256,
                    ref.size_bytes,
                    ref.kind,
                    ref.media_type,
                    ref.provenance,
                    ref.uri,
                    self._dump(ref.metadata),
                    content,
                    utc_now(),
                ),
            )
            # A worker may upload the declared parent artifact just after a
            # terminal receipt. Retry deferred child materialization here so
            # that artifact availability does not require a separate scheduler
            # process or an operator poke.
            predecessor = self._row_envelope(
                connection.execute("SELECT * FROM jobs WHERE task_id = ?", (task_id,)).fetchone()
            )
            if predecessor.state in TERMINAL_STATES:
                self._activate_pending_chain_steps(
                    connection,
                    predecessor_task_id=task_id,
                )
            return ref

    @staticmethod
    def _artifact_ref(row: sqlite3.Row) -> ArtifactRef:
        return ArtifactRef(
            artifact_id=row["artifact_id"],
            name=row["name"],
            sha256=row["sha256"],
            size_bytes=row["size_bytes"],
            kind=row["kind"],
            media_type=row["media_type"],
            provenance=row["provenance"],
            uri=row["uri"],
            metadata=RelayStore._load(row["metadata_json"]),
        )

    def list_artifacts(self, task_id: str) -> list[ArtifactRef]:
        self.get(task_id)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM artifacts WHERE task_id = ? ORDER BY created_at, artifact_id",
                (task_id,),
            ).fetchall()
            return [self._artifact_ref(row) for row in rows]

    def get_artifact(self, task_id: str, artifact_id: str) -> tuple[ArtifactRef, bytes]:
        self.get(task_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE task_id = ? AND artifact_id = ?",
                (task_id, artifact_id),
            ).fetchone()
            if row is None:
                raise StoreError(f"artifact {artifact_id} not found for task {task_id}")
            ref = self._artifact_ref(row)
            content = bytes(row["content"])
            if hashlib.sha256(content).hexdigest() != ref.sha256:
                raise StoreError(f"artifact {artifact_id} failed hash verification")
            return ref, content
