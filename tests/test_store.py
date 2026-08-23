from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agent_relay.protocol import AgentCard, JobEnvelope, JobReceipt, JobState, Readiness
from agent_relay.store import (
    IdempotencyConflict,
    ChainNotReady,
    JobNotFound,
    LeaseConflict,
    LeaseNotFound,
    RelayStore,
    StoreError,
)
from agent_relay.task import DelegationTask


def task(task_id: str = "store-task") -> DelegationTask:
    return DelegationTask(
        task_id=task_id,
        objective="Run one durable bounded task.",
        allowed_files=("value.py",),
        verification=("python -c \"assert True\"",),
        task_kind="mechanical",
    )


def terminal_receipt(task_id: str, state: JobState = JobState.CANCELLED) -> JobReceipt:
    return JobReceipt(
        receipt_id=f"receipt-{task_id}",
        task_id=task_id,
        final_state=state,
        actor="worker-a",
        completed_at="2026-08-23T00:00:00Z",
        evidence={"confirmed": True},
    )


def test_store_persists_envelope_events_and_idempotency_across_restart(tmp_path) -> None:
    database = tmp_path / "relay.sqlite3"
    first = RelayStore(database)
    envelope = JobEnvelope.new(task(), idempotency_key="same-request", requested_by="client-a")

    created, was_created = first.create_or_get(envelope)
    duplicate, duplicate_created = first.create_or_get(envelope)

    assert was_created is True
    assert duplicate_created is False
    assert duplicate.to_dict() == created.to_dict()

    restarted = RelayStore(database)
    restored = restarted.get("store-task")
    assert restored.task.objective == "Run one durable bounded task."
    assert [event.state for event in restarted.events_since("store-task")] == [JobState.SUBMITTED]

    with pytest.raises(IdempotencyConflict):
        restarted.create_or_get(
            JobEnvelope.new(task("different-task"), idempotency_key="same-request")
        )
    with pytest.raises(JobNotFound):
        restarted.get("missing")


def test_leases_require_ownership_and_are_renewable(tmp_path) -> None:
    store = RelayStore(tmp_path / "relay.sqlite3")
    store.create_or_get(JobEnvelope.new(task()))

    accepted, first_lease = store.acquire_lease("store-task", worker_id="worker-a", ttl_seconds=30)
    assert accepted.state is JobState.ACCEPTED
    running = store.transition(
        "store-task",
        JobState.RUNNING,
        actor="worker-a",
        lease_id=first_lease.lease_id,
        reason="started",
    )
    assert running.lease_id == first_lease.lease_id

    with pytest.raises(LeaseConflict):
        store.acquire_lease("store-task", worker_id="worker-b", ttl_seconds=30)
    with pytest.raises(LeaseConflict):
        store.transition(
            "store-task",
            JobState.WAITING,
            actor="worker-b",
            lease_id="wrong",
            reason="forged update",
        )

    renewed = store.renew_lease(
        "store-task",
        lease_id=first_lease.lease_id,
        worker_id="worker-a",
        ttl_seconds=45,
    )
    assert renewed.renewed is True
    assert store.get("store-task").lease_expires_at == renewed.expires_at

    released = store.release_lease(
        "store-task",
        lease_id=first_lease.lease_id,
        worker_id="worker-a",
    )
    assert released.lease_id is None
    assert released.state is JobState.WAITING
    assert released.worker_id is None
    with pytest.raises(LeaseNotFound):
        store.transition(
            "store-task",
            JobState.RUNNING,
            actor="worker-a",
            lease_id=first_lease.lease_id,
            reason="stale worker",
        )


def test_claude_success_requires_deterministic_and_sol_review_receipts(tmp_path) -> None:
    store = RelayStore(tmp_path / "relay.sqlite3")
    store.create_or_get(JobEnvelope.new(task("claude-acceptance")))
    accepted, lease = store.acquire_lease(
        "claude-acceptance",
        worker_id="worker-a",
        ttl_seconds=30,
    )
    store.transition(
        "claude-acceptance",
        JobState.RUNNING,
        actor="worker-a",
        lease_id=lease.lease_id,
        reason="started",
    )
    missing_review = JobReceipt(
        receipt_id="receipt-missing-sol-review",
        task_id="claude-acceptance",
        final_state=JobState.SUCCEEDED,
        actor="worker-a",
        completed_at="2026-08-23T00:00:00Z",
        evidence={"lane": "claude-task"},
        verification=({"command": "pytest -q", "passed": True},),
    )
    with pytest.raises(StoreError, match="sol-reviewer"):
        store.transition(
            "claude-acceptance",
            JobState.SUCCEEDED,
            actor="worker-a",
            lease_id=lease.lease_id,
            reason="worker returned terminal result",
            receipt=missing_review,
        )

    accepted_receipt = JobReceipt(
        receipt_id="receipt-with-sol-review",
        task_id="claude-acceptance",
        final_state=JobState.SUCCEEDED,
        actor="worker-a",
        completed_at="2026-08-23T00:00:00Z",
        evidence={
            "lane": "claude-task",
            "sol_review": {"lane": "sol-reviewer", "status": "PASS"},
        },
        verification=({"command": "pytest -q", "passed": True},),
    )
    succeeded = store.transition(
        "claude-acceptance",
        JobState.SUCCEEDED,
        actor="worker-a",
        lease_id=lease.lease_id,
        reason="worker returned terminal result",
        evidence=dict(accepted_receipt.evidence),
        receipt=accepted_receipt,
    )
    assert succeeded.state is JobState.SUCCEEDED


def test_released_running_task_is_claimable_by_another_worker(tmp_path) -> None:
    store = RelayStore(tmp_path / "relay.sqlite3")
    store.create_or_get(JobEnvelope.new(task()))
    _, lease = store.acquire_lease("store-task", worker_id="worker-a", ttl_seconds=30)
    store.transition(
        "store-task",
        JobState.RUNNING,
        actor="worker-a",
        lease_id=lease.lease_id,
        reason="started",
    )

    released = store.release_lease(
        "store-task",
        lease_id=lease.lease_id,
        worker_id="worker-a",
    )
    reassigned, new_lease = store.acquire_lease(
        "store-task",
        worker_id="worker-b",
        ttl_seconds=30,
    )

    assert released.state is JobState.WAITING
    assert reassigned.worker_id == "worker-b"
    assert new_lease.worker_id == "worker-b"


def test_expired_lease_returns_running_task_to_waiting_before_reassignment(tmp_path) -> None:
    database = tmp_path / "relay.sqlite3"
    store = RelayStore(database)
    store.create_or_get(JobEnvelope.new(task()))
    _, lease = store.acquire_lease("store-task", worker_id="worker-a", ttl_seconds=30)
    store.transition(
        "store-task",
        JobState.RUNNING,
        actor="worker-a",
        lease_id=lease.lease_id,
        reason="started",
    )

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE leases SET expires_at = ? WHERE task_id = ?",
            ("2000-01-01T00:00:00Z", "store-task"),
        )
        connection.commit()

    reassigned, new_lease = store.acquire_lease("store-task", worker_id="worker-b", ttl_seconds=30)

    assert reassigned.state is JobState.ACCEPTED
    assert reassigned.worker_id == "worker-b"
    assert new_lease.worker_id == "worker-b"
    assert any("lease expired" in event.reason for event in reassigned.events)


def test_cancel_request_is_distinct_from_confirmed_cancel(tmp_path) -> None:
    store = RelayStore(tmp_path / "relay.sqlite3")
    store.create_or_get(JobEnvelope.new(task()))

    not_started = store.cancel("store-task")
    assert not_started.state is JobState.CANCELLED
    assert not_started.receipt is not None
    assert not_started.receipt.evidence["execution_started"] is False

    running_task = task("running-cancel-task")
    store.create_or_get(JobEnvelope.new(running_task))
    _, lease = store.acquire_lease("running-cancel-task", worker_id="worker-a", ttl_seconds=30)
    store.transition(
        "running-cancel-task",
        JobState.RUNNING,
        actor="worker-a",
        lease_id=lease.lease_id,
        reason="worker started",
    )
    requested = store.cancel("running-cancel-task")
    assert requested.state is JobState.CANCEL_REQUESTED
    assert requested.receipt is None

    cancelled = store.transition(
        "running-cancel-task",
        JobState.CANCELLED,
        actor="worker-a",
        lease_id=lease.lease_id,
        reason="worker confirmed stop",
        evidence={"execution_stopped": True},
        receipt=terminal_receipt("running-cancel-task", JobState.CANCELLED),
    )
    assert cancelled.state is JobState.CANCELLED
    assert cancelled.receipt is not None
    assert {item.task_id for item in store.list_jobs(state=JobState.CANCELLED)} == {"store-task", "running-cancel-task"}


def test_expired_lease_after_cancel_request_becomes_explicitly_blocked(tmp_path) -> None:
    database = tmp_path / "relay.sqlite3"
    store = RelayStore(database)
    store.create_or_get(JobEnvelope.new(task("cancel-expired-task")))
    _, lease = store.acquire_lease("cancel-expired-task", worker_id="worker-a", ttl_seconds=30)
    store.transition(
        "cancel-expired-task",
        JobState.RUNNING,
        actor="worker-a",
        lease_id=lease.lease_id,
        reason="worker started",
    )
    requested = store.cancel("cancel-expired-task")
    assert requested.state is JobState.CANCEL_REQUESTED

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE leases SET expires_at = ? WHERE task_id = ?",
            ("2000-01-01T00:00:00Z", "cancel-expired-task"),
        )
        connection.commit()

    with pytest.raises(LeaseConflict, match="blocked because cancellation"):
        store.acquire_lease("cancel-expired-task", worker_id="worker-b", ttl_seconds=30)

    blocked = store.get("cancel-expired-task")
    assert blocked.state is JobState.BLOCKED
    assert blocked.receipt is not None
    assert blocked.receipt.evidence["execution_stopped"] is False


def test_resume_requeues_waiting_task_and_agent_registry_survives_restart(tmp_path) -> None:
    database = tmp_path / "relay.sqlite3"
    store = RelayStore(database)
    store.create_or_get(JobEnvelope.new(task()))
    _, lease = store.acquire_lease("store-task", worker_id="worker-a", ttl_seconds=30)
    store.transition(
        "store-task",
        JobState.RUNNING,
        actor="worker-a",
        lease_id=lease.lease_id,
        reason="started",
    )
    store.transition(
        "store-task",
        JobState.WAITING,
        actor="worker-a",
        lease_id=lease.lease_id,
        reason="worker disconnected",
    )
    resumed = store.resume("store-task")
    assert resumed.state is JobState.ACCEPTED
    assert resumed.lease_id is None

    card = AgentCard(
        agent_id="worker-a",
        name="Worker A",
        readiness=Readiness.READY,
        capabilities=("bounded-edit",),
        task_kinds=("mechanical",),
        transports=("a2a-http",),
    )
    store.register_agent(card)
    restarted = RelayStore(database)
    assert restarted.get_agent("worker-a") == card
    assert restarted.list_agents()[0].agent_id == "worker-a"
    assert restarted.list_agents(task_kind="mechanical", capability="bounded-edit", readiness="ready")[0].agent_id == "worker-a"
    heartbeat = restarted.heartbeat_agent("worker-a", readiness="degraded", metadata={"last_task_id": "store-task"})
    assert heartbeat.readiness is Readiness.DEGRADED
    assert heartbeat.metadata["last_task_id"] == "store-task"
    assert restarted.list_agents(readiness="ready") == []


def test_claim_next_routes_oldest_compatible_task_and_skips_incompatible_work(tmp_path) -> None:
    store = RelayStore(tmp_path / "relay.sqlite3")
    store.register_agent(
        AgentCard(
            agent_id="worker-a",
            name="Local worker",
            readiness=Readiness.UNKNOWN,
            capabilities=("bounded-edit", "ollama"),
            task_kinds=("mechanical",),
            transports=("a2a-http",),
            metadata={"backend": "local-qwen"},
        )
    )
    store.create_or_get(JobEnvelope.new(task("claude-only"), workspace_policy={"backend": "claude-task"}))
    store.create_or_get(JobEnvelope.new(task("local-task"), workspace_policy={"backend": "local-qwen"}))

    claimed = store.claim_next(worker_id="worker-a", ttl_seconds=30)

    assert claimed is not None
    envelope, lease = claimed
    assert envelope.task_id == "local-task"
    assert envelope.worker_id == "worker-a"
    assert lease.worker_id == "worker-a"
    assert store.claim_next(worker_id="worker-a", ttl_seconds=30) is None


def test_claim_next_prioritizes_work_and_expires_unleased_deadlines(tmp_path) -> None:
    store = RelayStore(tmp_path / "relay.sqlite3")
    store.register_agent(
        AgentCard(
            agent_id="worker-a",
            name="Local worker",
            readiness=Readiness.UNKNOWN,
            capabilities=("bounded-edit", "ollama"),
            task_kinds=("mechanical",),
            transports=("a2a-http",),
            metadata={"backend": "local-qwen"},
        )
    )
    overdue = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    store.create_or_get(JobEnvelope.new(task("expired-task"), deadline_at=overdue))
    store.create_or_get(JobEnvelope.new(task("low-priority"), priority=1))
    store.create_or_get(JobEnvelope.new(task("high-priority"), priority=9))

    claimed = store.claim_next(worker_id="worker-a", ttl_seconds=30)

    assert claimed is not None
    assert claimed[0].task_id == "high-priority"
    expired = store.get("expired-task")
    assert expired.state is JobState.EXPIRED
    assert expired.receipt is not None
    assert expired.receipt.evidence["expired_by"] == "coordinator_scheduler"


def test_chain_step_is_terminal_gated_idempotent_and_restartable(tmp_path) -> None:
    database = tmp_path / "relay.sqlite3"
    store = RelayStore(database)
    parent_task = task("chain-parent")
    parent, created = store.submit_chain_step(
        chain_id="chain-1",
        step_id="build",
        step_index=0,
        task=parent_task,
        parent_messages=(),
    )
    assert created is True
    artifact = store.put_artifact(
        parent.task_id,
        name="build.patch",
        content=b"diff --git a/value.py b/value.py\n",
        kind="patch",
        media_type="text/x-diff",
        provenance="worker-a",
    )
    _, lease = store.acquire_lease(parent.task_id, worker_id="worker-a", ttl_seconds=30)
    store.transition(
        parent.task_id,
        JobState.RUNNING,
        actor="worker-a",
        lease_id=lease.lease_id,
        reason="started",
    )
    store.transition(
        parent.task_id,
        JobState.SUCCEEDED,
        actor="worker-a",
        lease_id=lease.lease_id,
        reason="verified",
        evidence={"verified": True},
        receipt=terminal_receipt(parent.task_id, JobState.SUCCEEDED),
    )

    child, child_created = store.submit_chain_step(
        chain_id="chain-1",
        step_id="review",
        step_index=1,
        task=task("chain-child"),
        predecessor_task_id=parent.task_id,
        parent_artifact_ids=(artifact.artifact_id,),
        parent_messages=("Review only the declared build patch.",),
    )
    duplicate, duplicate_created = store.submit_chain_step(
        chain_id="chain-1",
        step_id="review",
        step_index=1,
        task=task("chain-child"),
        predecessor_task_id=parent.task_id,
        parent_artifact_ids=(artifact.artifact_id,),
        parent_messages=("Review only the declared build patch.",),
    )

    assert child_created is True
    assert duplicate_created is False
    assert duplicate.to_dict() == child.to_dict()
    assert child.chain_id == "chain-1"
    assert child.chain_step_index == 1
    assert child.parent_artifacts[0].artifact_id == artifact.artifact_id
    assert child.parent_messages == ("Review only the declared build patch.",)
    restarted = RelayStore(database)
    chain = restarted.get_chain("chain-1")
    assert [step["step_id"] for step in chain["steps"]] == ["build", "review"]


def test_chain_step_rejects_live_predecessor_unless_terminal_policy_is_met(tmp_path) -> None:
    store = RelayStore(tmp_path / "relay.sqlite3")
    parent, _ = store.submit_chain_step(
        chain_id="chain-2",
        step_id="build",
        step_index=0,
        task=task("chain-live-parent"),
    )
    with pytest.raises(ChainNotReady, match="allowed states"):
        store.submit_chain_step(
            chain_id="chain-2",
            step_id="review",
            step_index=1,
            task=task("chain-live-child"),
            predecessor_task_id=parent.task_id,
        )
    _, lease = store.acquire_lease(parent.task_id, worker_id="worker-a", ttl_seconds=30)
    store.transition(
        parent.task_id,
        JobState.RUNNING,
        actor="worker-a",
        lease_id=lease.lease_id,
        reason="started",
    )
    store.transition(
        parent.task_id,
        JobState.FAILED,
        actor="worker-a",
        lease_id=lease.lease_id,
        reason="adapter failed",
        evidence={"worker_error": "bounded test failure"},
        receipt=terminal_receipt(parent.task_id, JobState.FAILED),
    )
    child, created = store.submit_chain_step(
        chain_id="chain-2",
        step_id="diagnose",
        step_index=1,
        task=task("chain-failed-child"),
        predecessor_task_id=parent.task_id,
        allowed_predecessor_states=(JobState.FAILED,),
    )
    assert created is True
    assert child.predecessor_task_id == parent.task_id


def test_deferred_chain_step_auto_materializes_on_parent_terminal_and_artifact(tmp_path) -> None:
    store = RelayStore(tmp_path / "relay.sqlite3")
    parent, _ = store.submit_chain_step(
        chain_id="chain-deferred",
        step_id="build",
        step_index=0,
        task=task("deferred-parent"),
    )
    _, lease = store.acquire_lease(parent.task_id, worker_id="worker-a", ttl_seconds=30)
    store.transition(
        parent.task_id,
        JobState.RUNNING,
        actor="worker-a",
        lease_id=lease.lease_id,
        reason="started",
    )
    artifact = store.put_artifact(
        parent.task_id,
        name="build.patch",
        content=b"patch",
        kind="patch",
        media_type="text/plain",
        provenance="worker-a",
    )
    scheduled = store.schedule_chain_step(
        chain_id="chain-deferred",
        step_id="review",
        step_index=1,
        task=task("deferred-child"),
        predecessor_task_id=parent.task_id,
        parent_artifact_ids=(artifact.artifact_id,),
        priority=8,
        deadline_at="2027-08-23T00:00:00Z",
    )
    assert scheduled.pending is True
    assert scheduled.envelope is None

    store.transition(
        parent.task_id,
        JobState.SUCCEEDED,
        actor="worker-a",
        lease_id=lease.lease_id,
        reason="verified",
        evidence={"verified": True},
        receipt=terminal_receipt(parent.task_id, JobState.SUCCEEDED),
    )

    child = store.get("deferred-child")
    assert child.predecessor_task_id == parent.task_id
    assert child.parent_artifacts[0].artifact_id == artifact.artifact_id
    assert child.priority == 8
    assert child.deadline_at == "2027-08-23T00:00:00Z"
    chain = store.get_chain("chain-deferred")
    assert [step["step_id"] for step in chain["steps"]] == ["build", "review"]
    assert chain["pending_steps"][0]["status"] == "materialized"


def test_deferred_chain_step_rejects_changed_idempotent_payload(tmp_path) -> None:
    store = RelayStore(tmp_path / "relay.sqlite3")
    parent, _ = store.submit_chain_step(
        chain_id="chain-idempotent-deferred",
        step_id="build",
        step_index=0,
        task=task("idempotent-deferred-parent"),
    )
    first = store.schedule_chain_step(
        chain_id="chain-idempotent-deferred",
        step_id="review",
        step_index=1,
        task=task("idempotent-deferred-child"),
        predecessor_task_id=parent.task_id,
        parent_messages=("first",),
    )
    assert first.pending is True
    with pytest.raises(IdempotencyConflict):
        store.schedule_chain_step(
            chain_id="chain-idempotent-deferred",
            step_id="review",
            step_index=1,
            task=task("idempotent-deferred-child"),
            predecessor_task_id=parent.task_id,
            parent_messages=("changed",),
        )


def test_bad_deferred_recipe_cannot_roll_back_parent_terminal_state(tmp_path) -> None:
    database = tmp_path / "relay.sqlite3"
    store = RelayStore(database)
    parent, _ = store.submit_chain_step(
        chain_id="chain-bad-recipe",
        step_id="build",
        step_index=0,
        task=task("bad-recipe-parent"),
    )
    store.schedule_chain_step(
        chain_id="chain-bad-recipe",
        step_id="review",
        step_index=1,
        task=task("bad-recipe-child"),
        predecessor_task_id=parent.task_id,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE pending_chain_steps SET request_json = ? WHERE chain_id = ? AND step_id = ?",
            ('{"task":{"task_id":"bad-recipe-child"}}', "chain-bad-recipe", "review"),
        )
        connection.commit()
    _, lease = store.acquire_lease(parent.task_id, worker_id="worker-a", ttl_seconds=30)
    store.transition(parent.task_id, JobState.RUNNING, actor="worker-a", lease_id=lease.lease_id, reason="started")
    terminal = store.transition(
        parent.task_id,
        JobState.SUCCEEDED,
        actor="worker-a",
        lease_id=lease.lease_id,
        reason="verified",
        evidence={"verified": True},
        receipt=terminal_receipt(parent.task_id, JobState.SUCCEEDED),
    )
    assert terminal.state is JobState.SUCCEEDED
    assert store.get_chain("chain-bad-recipe")["pending_steps"][0]["status"] == "blocked"
