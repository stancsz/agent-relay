from __future__ import annotations

import hashlib

import pytest

from agent_relay.protocol import (
    AgentCard,
    ArtifactRef,
    JobEnvelope,
    JobReceipt,
    JobState,
    ProtocolError,
    Readiness,
    SCHEMA_VERSION,
    protocol_digest,
)
from agent_relay.task import DelegationTask


def task() -> DelegationTask:
    return DelegationTask(
        task_id="protocol-task",
        objective="Change the bounded value.",
        allowed_files=("src/value.py",),
        context=("README.md:10-20",),
        requirements=("Keep the public function stable.",),
        constraints=("Do not touch unrelated files.",),
        verification=("python -c \"assert True\"",),
        success_criteria=("The value is two.",),
        model="qwen3.5:4b",
        retry_limit=1,
        context_mode="replace",
        task_kind="bounded_bugfix",
        risk_flags=("no_network",),
    )


def receipt(final_state: JobState = JobState.SUCCEEDED) -> JobReceipt:
    digest = hashlib.sha256(b"patch").hexdigest()
    return JobReceipt(
        receipt_id="receipt-protocol-task",
        task_id="protocol-task",
        final_state=final_state,
        actor="worker-a",
        completed_at="2026-08-23T00:00:00Z",
        evidence={"worker_status": "done", "scope_checked": True},
        artifacts=(
            ArtifactRef(
                artifact_id="artifact-1",
                name="change.patch",
                sha256=digest,
                size_bytes=5,
                kind="patch",
                media_type="text/x-diff",
            ),
        ),
        verification=({"command": "python -c assert True", "passed": True},),
        workspace={"before": "abc", "after": "def"},
        summary="verified",
    )


def test_envelope_round_trip_preserves_policy_and_history() -> None:
    envelope = JobEnvelope.new(
        task(),
        idempotency_key="idem-protocol-task",
        requested_by="client-a",
        workspace_policy={"repository": "demo", "allow_edits": True},
        priority=7,
        deadline_at="2026-08-23T03:02:03+02:00",
    )
    envelope = envelope.transition(
        JobState.ACCEPTED,
        actor="coordinator",
        reason="worker selected",
    )
    envelope = envelope.assign_lease(
        worker_id="worker-a",
        lease_id="lease-1",
        lease_expires_at="2026-08-23T00:05:00Z",
    )
    envelope = envelope.transition(
        JobState.RUNNING,
        actor="worker-a",
        reason="execution started",
        progress=0.25,
    )
    terminal = envelope.transition(
        JobState.SUCCEEDED,
        actor="worker-a",
        reason="verification passed",
        evidence={"verification": "passed"},
        receipt=receipt(),
        progress=1,
    )

    payload = terminal.to_dict()
    restored = JobEnvelope.from_dict(payload)

    assert payload["schema"] == SCHEMA_VERSION
    assert restored.task.to_dict() == task().to_dict()
    assert restored.idempotency_key == "idem-protocol-task"
    assert restored.workspace_policy["allow_edits"] is True
    assert restored.state is JobState.SUCCEEDED
    assert len(restored.events) == 5
    assert restored.receipt is not None
    assert restored.receipt.artifacts[0].sha256 == hashlib.sha256(b"patch").hexdigest()
    assert restored.priority == 7
    assert restored.deadline_at == "2026-08-23T01:02:03Z"
    assert protocol_digest(payload) == protocol_digest(restored.to_dict())


def test_scheduling_fields_are_bounded_and_deadline_requires_timezone() -> None:
    with pytest.raises(ProtocolError, match="priority"):
        JobEnvelope.new(task(), priority=1001)
    with pytest.raises(ProtocolError, match="deadline_at"):
        JobEnvelope.new(task(), deadline_at="2026-08-23T03:02:03")


def test_terminal_transition_requires_matching_evidence_and_receipt() -> None:
    envelope = JobEnvelope.new(task()).transition(
        JobState.ACCEPTED,
        actor="coordinator",
        reason="accepted",
    )

    with pytest.raises(ProtocolError, match="non-empty evidence"):
        envelope.transition(
            JobState.FAILED,
            actor="worker-a",
            reason="failed",
            receipt=receipt(JobState.FAILED),
        )

    with pytest.raises(ProtocolError, match="requires a receipt"):
        envelope.transition(
            JobState.FAILED,
            actor="worker-a",
            reason="failed",
            evidence={"error": "boom"},
        )

    with pytest.raises(ProtocolError, match="does not match"):
        envelope.transition(
            JobState.FAILED,
            actor="worker-a",
            reason="failed",
            evidence={"error": "boom"},
            receipt=receipt(JobState.SUCCEEDED),
        )


def test_invalid_transition_and_tampered_envelope_are_rejected() -> None:
    envelope = JobEnvelope.new(task())
    with pytest.raises(ProtocolError, match="invalid transition"):
        envelope.transition(JobState.SUCCEEDED, actor="worker-a", reason="too early")

    accepted = envelope.transition(JobState.ACCEPTED, actor="coordinator", reason="accepted")
    payload = accepted.to_dict()
    payload["state"] = JobState.RUNNING.value
    with pytest.raises(ProtocolError, match="envelope_sha256"):
        JobEnvelope.from_dict(payload)

    payload = accepted.to_dict()
    payload.pop("envelope_sha256")
    payload["events"].append(
        {
            **payload["events"][-1],
            "event_id": "forged",
            "state": JobState.SUCCEEDED.value,
        }
    )
    payload["state"] = JobState.SUCCEEDED.value
    payload["receipt"] = receipt().to_dict()
    with pytest.raises(ProtocolError, match="invalid transition"):
        JobEnvelope.from_dict(payload)


def test_agent_card_readiness_is_explicit_and_round_trips() -> None:
    card = AgentCard(
        agent_id="worker-a",
        name="Claude worker",
        readiness=Readiness.DEGRADED,
        capabilities=("bounded-edit", "verification"),
        task_kinds=("bounded_bugfix",),
        transports=("a2a-http",),
        workspace={"os": "windows", "sandbox": "git-copy"},
        artifact_limits={"max_bytes": 1_000_000},
    )

    restored = AgentCard.from_dict(card.to_dict())

    assert restored == card
    with pytest.raises(ProtocolError, match="readiness"):
        AgentCard.from_dict({"agent_id": "x", "name": "x", "readiness": "ready-ish"})


def test_receipt_rejects_invalid_artifact_digest() -> None:
    with pytest.raises(ProtocolError, match="sha256"):
        ArtifactRef(
            artifact_id="bad",
            name="bad.txt",
            sha256="not-a-digest",
            size_bytes=1,
        )
