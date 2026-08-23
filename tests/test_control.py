from __future__ import annotations

from threading import Thread
from urllib.request import Request, urlopen

import pytest

from agent_relay.control import ControlPlaneError, create_server, request_json, stream_events
from agent_relay.protocol import JobState


def task_payload(task_id: str = "http-task") -> dict:
    return {
        "task_id": task_id,
        "objective": "Run one durable HTTP task.",
        "allowed_files": ["value.py"],
        "verification": ["python -c \"assert True\""],
        "task_kind": "mechanical",
    }


def run_server(tmp_path, *, token: str | None = "secret"):
    server = create_server(
        host="127.0.0.1",
        port=0,
        database=tmp_path / "relay.sqlite3",
        auth_token=token,
    )
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    return server, thread, base


def stop_server(server, thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_health_is_public_but_task_api_requires_bearer_auth(tmp_path) -> None:
    server, thread, base = run_server(tmp_path)
    try:
        health = request_json(base, "GET", "/health")
        assert health["healthy"] is True
        assert health["auth_required"] is True
        assert health["tls"] is False

        with pytest.raises(ControlPlaneError, match="401"):
            request_json(base, "GET", "/tasks")
        with pytest.raises(ControlPlaneError, match="401"):
            request_json(base, "POST", "/tasks", payload={"task": task_payload()})
    finally:
        stop_server(server, thread)


def test_non_loopback_server_requires_authentication(tmp_path) -> None:
    with pytest.raises(ControlPlaneError, match="non-loopback"):
        create_server(
            host="0.0.0.0",
            port=0,
            database=tmp_path / "relay.sqlite3",
            auth_token=None,
        )


def test_tls_configuration_requires_a_certificate_and_key(tmp_path) -> None:
    with pytest.raises(ControlPlaneError, match="tls_cert and tls_key"):
        create_server(
            host="127.0.0.1",
            port=0,
            database=tmp_path / "relay.sqlite3",
            auth_token="secret",
            tls_cert=tmp_path / "server.pem",
        )
    with pytest.raises(ControlPlaneError, match="could not enable coordinator TLS"):
        create_server(
            host="127.0.0.1",
            port=0,
            database=tmp_path / "relay-invalid.sqlite3",
            auth_token="secret",
            tls_cert=tmp_path / "server.pem",
            tls_key=tmp_path / "server.key",
        )


def test_submit_is_idempotent_and_survives_coordinator_restart(tmp_path) -> None:
    server, thread, base = run_server(tmp_path)
    try:
        first = request_json(
            base,
            "POST",
            "/tasks",
            auth_token="secret",
            payload={
                "task": task_payload(),
                "idempotency_key": "http-idempotency",
                "requested_by": "test-client",
                "priority": 6,
                "deadline_at": "2027-08-23T00:00:00Z",
            },
        )
        duplicate = request_json(
            base,
            "POST",
            "/tasks",
            auth_token="secret",
            payload={
                "task": task_payload(),
                "idempotency_key": "http-idempotency",
                "priority": 6,
                "deadline_at": "2027-08-23T00:00:00Z",
            },
        )
        assert first["created"] is True
        assert duplicate["created"] is False
        assert duplicate["task_id"] == first["task_id"] == "http-task"
        assert first["envelope"]["priority"] == 6
        assert first["envelope"]["deadline_at"] == "2027-08-23T00:00:00Z"
    finally:
        stop_server(server, thread)

    server, thread, base = run_server(tmp_path)
    try:
        inspected = request_json(base, "GET", "/tasks/http-task", auth_token="secret")
        assert inspected["state"] == JobState.SUBMITTED.value
        assert inspected["task"]["objective"] == "Run one durable HTTP task."
    finally:
        stop_server(server, thread)


def test_http_lifecycle_carries_lease_events_and_terminal_receipt(tmp_path) -> None:
    server, thread, base = run_server(tmp_path, token=None)
    try:
        request_json(base, "POST", "/tasks", payload={"task": task_payload()})
        leased = request_json(
            base,
            "POST",
            "/tasks/http-task/leases",
            payload={"worker_id": "worker-a", "ttl_seconds": 30},
        )
        lease_id = leased["lease"]["lease_id"]
        assert leased["envelope"]["state"] == JobState.ACCEPTED.value

        running = request_json(
            base,
            "POST",
            "/tasks/http-task/transition",
            payload={
                "state": "running",
                "actor": "worker-a",
                "lease_id": lease_id,
                "reason": "started",
                "progress": 0.5,
            },
        )
        assert running["state"] == JobState.RUNNING.value

        renewed = request_json(
            base,
            "POST",
            "/tasks/http-task/leases/renew",
            payload={"worker_id": "worker-a", "lease_id": lease_id, "ttl_seconds": 30},
        )
        assert renewed["lease"]["renewed"] is True

        uploaded = request_json(
            base,
            "POST",
            "/tasks/http-task/artifacts",
            payload={
                "name": "change.patch",
                "content": "diff --git a/value.py b/value.py\n",
                "kind": "patch",
                "media_type": "text/x-diff",
                "provenance": "worker-a",
            },
        )
        artifact = uploaded["artifact"]
        downloaded = request_json(
            base,
            "GET",
            f"/tasks/http-task/artifacts/{artifact['artifact_id']}",
        )
        assert downloaded["artifact"]["sha256"] == artifact["sha256"]
        assert downloaded["content_base64"]

        requested = request_json(
            base,
            "POST",
            "/tasks/http-task/cancel",
            payload={"actor": "client"},
        )
        assert requested["state"] == JobState.CANCEL_REQUESTED.value

        receipt = {
            "task_id": "http-task",
            "receipt_id": "receipt-http-task",
            "final_state": "cancelled",
            "actor": "worker-a",
            "completed_at": "2026-08-23T00:00:00Z",
            "evidence": {"execution_stopped": True},
            "artifacts": [artifact],
        }
        cancelled = request_json(
            base,
            "POST",
            "/tasks/http-task/transition",
            payload={
                "state": "cancelled",
                "actor": "worker-a",
                "lease_id": lease_id,
                "reason": "stop confirmed",
                "evidence": {"execution_stopped": True},
                "receipt": receipt,
            },
        )
        assert cancelled["state"] == JobState.CANCELLED.value
        assert cancelled["receipt"]["final_state"] == "cancelled"

        events = request_json(base, "GET", "/tasks/http-task/events?after=0")
        assert [event["state"] for event in events["events"]] == [
            "submitted",
            "accepted",
            "accepted",
            "running",
            "running",
            "cancel_requested",
            "cancelled",
        ]
        stream_request = Request(
            base + "/tasks/http-task/events/stream?after=0&timeout=1",
            headers={"Authorization": "Bearer secret", "Accept": "text/event-stream"},
        )
        with urlopen(stream_request, timeout=3) as response:
            stream = response.read().decode("utf-8")
        assert "event_id" in stream
        assert "data:" in stream
        assert "cancelled" in stream
        decoded = list(stream_events(base, "/tasks/http-task/events/stream?after=0&timeout=1", auth_token="secret"))
        assert decoded[-1]["data"]["state"] == "cancelled"
    finally:
        stop_server(server, thread)


def test_agent_registration_is_machine_readable(tmp_path) -> None:
    server, thread, base = run_server(tmp_path, token=None)
    try:
        card = {
            "agent_id": "worker-a",
            "name": "Worker A",
            "readiness": "ready",
            "capabilities": ["bounded-edit"],
            "task_kinds": ["mechanical"],
            "transports": ["a2a-http"],
        }
        registered = request_json(base, "POST", "/agents/register", payload=card)
        listed = request_json(base, "GET", "/agents")
        filtered = request_json(base, "GET", "/agents?task_kind=mechanical&capability=bounded-edit&readiness=ready")
        heartbeat = request_json(
            base,
            "POST",
            "/agents/worker-a/heartbeat",
            payload={"readiness": "degraded", "metadata": {"probe": "dependency unavailable"}},
        )
        assert registered["agent"]["agent_id"] == "worker-a"
        assert listed["agents"][0]["readiness"] == "ready"
        assert [item["agent_id"] for item in filtered["agents"]] == ["worker-a"]
        assert heartbeat["agent"]["readiness"] == "degraded"
        assert heartbeat["agent"]["metadata"]["probe"] == "dependency unavailable"
    finally:
        stop_server(server, thread)


def test_scoped_worker_credential_can_mutate_only_as_enrolled_agent(tmp_path) -> None:
    server, thread, base = run_server(tmp_path, token="admin-secret")
    agent_headers = {"agent_id": "worker-a", "agent_token": "worker-secret"}
    try:
        card = {
            "agent_id": "worker-a",
            "name": "Worker A",
            "readiness": "unknown",
            "capabilities": ["bounded-edit"],
            "task_kinds": ["mechanical"],
            "transports": ["a2a-http"],
        }
        registered = request_json(base, "POST", "/agents/register", payload=card, auth_token="admin-secret", **agent_headers)
        assert "agent_token" not in registered["agent"]
        refreshed = request_json(base, "POST", "/agents/register", payload=card, **agent_headers)
        assert refreshed["agent"]["agent_id"] == "worker-a"

        request_json(
            base,
            "POST",
            "/tasks",
            payload={"task": task_payload(), "idempotency_key": "scoped-worker-task"},
            auth_token="admin-secret",
        )
        leased = request_json(
            base,
            "POST",
            "/tasks/http-task/leases",
            payload={"worker_id": "worker-a", "ttl_seconds": 30},
            **agent_headers,
        )
        assert leased["lease"]["worker_id"] == "worker-a"
        running = request_json(
            base,
            "POST",
            "/tasks/http-task/transition",
            payload={
                "state": "running",
                "actor": "worker-a",
                "lease_id": leased["lease"]["lease_id"],
                "reason": "scoped worker started",
            },
            **agent_headers,
        )
        assert running["state"] == "running"
        listing = request_json(base, "GET", "/tasks", **agent_headers)
        assert listing["tasks"][0]["task"]["task_id"] == "http-task"

        request_json(base, "POST", "/agents/worker-a/revoke", payload={}, auth_token="admin-secret")
        with pytest.raises(ControlPlaneError, match="401"):
            request_json(base, "POST", "/agents/worker-a/heartbeat", payload={}, **agent_headers)
        with pytest.raises(ControlPlaneError, match="401"):
            request_json(base, "GET", "/tasks", **agent_headers)
    finally:
        stop_server(server, thread)


def test_http_release_returns_active_task_to_waiting(tmp_path) -> None:
    server, thread, base = run_server(tmp_path, token=None)
    try:
        request_json(base, "POST", "/tasks", payload={"task": task_payload()})
        leased = request_json(
            base,
            "POST",
            "/tasks/http-task/leases",
            payload={"worker_id": "worker-a", "ttl_seconds": 30},
        )
        lease_id = leased["lease"]["lease_id"]
        request_json(
            base,
            "POST",
            "/tasks/http-task/transition",
            payload={
                "state": "running",
                "actor": "worker-a",
                "lease_id": lease_id,
                "reason": "started",
            },
        )

        released = request_json(
            base,
            "POST",
            "/tasks/http-task/leases/release",
            payload={"worker_id": "worker-a", "lease_id": lease_id},
        )

        assert released["state"] == JobState.WAITING.value
        assert released["lease_id"] is None
        reassigned = request_json(
            base,
            "POST",
            "/tasks/http-task/leases",
            payload={"worker_id": "worker-b", "ttl_seconds": 30},
        )
        assert reassigned["lease"]["worker_id"] == "worker-b"
    finally:
        stop_server(server, thread)


def test_scoped_worker_cannot_claim_task_outside_agent_card_policy(tmp_path) -> None:
    server, thread, base = run_server(tmp_path, token="admin-secret")
    agent_headers = {"agent_id": "worker-a", "agent_token": "worker-secret"}
    try:
        card = {
            "agent_id": "worker-a",
            "name": "Local Qwen Worker",
            "readiness": "ready",
            "capabilities": ["bounded-edit", "ollama"],
            "task_kinds": ["mechanical"],
            "transports": ["agent-relay-http"],
            "metadata": {"backend": "local-qwen"},
        }
        request_json(
            base,
            "POST",
            "/agents/register",
            payload=card,
            auth_token="admin-secret",
            **agent_headers,
        )
        request_json(
            base,
            "POST",
            "/tasks",
            payload={
                "task": task_payload(),
                "workspace_policy": {
                    "backend": "claude-task",
                    "required_capabilities": ["claude-a2a"],
                },
            },
            auth_token="admin-secret",
        )

        with pytest.raises(ControlPlaneError, match="422"):
            request_json(
                base,
                "POST",
                "/tasks/http-task/leases",
                payload={"worker_id": "worker-a", "ttl_seconds": 30},
                **agent_headers,
            )
    finally:
        stop_server(server, thread)


def test_scoped_worker_cannot_read_or_upload_across_tasks(tmp_path) -> None:
    server, thread, base = run_server(tmp_path, token="admin-secret")
    agent_headers = {"agent_id": "worker-a", "agent_token": "worker-secret"}
    try:
        request_json(
            base,
            "POST",
            "/agents/register",
            payload={
                "agent_id": "worker-a",
                "name": "Worker A",
                "readiness": "ready",
                "capabilities": ["bounded-edit"],
                "task_kinds": ["mechanical"],
                "transports": ["a2a-http"],
            },
            auth_token="admin-secret",
            **agent_headers,
        )
        request_json(base, "POST", "/tasks", payload={"task": task_payload("owned-task")}, auth_token="admin-secret")
        request_json(base, "POST", "/tasks", payload={"task": task_payload("other-task")}, auth_token="admin-secret")
        leased = request_json(
            base,
            "POST",
            "/tasks/owned-task/leases",
            payload={"worker_id": "worker-a", "ttl_seconds": 30},
            **agent_headers,
        )
        scoped_tasks = request_json(base, "GET", "/tasks", **agent_headers)
        assert [item["task"]["task_id"] for item in scoped_tasks["tasks"]] == ["owned-task"]
        with pytest.raises(ControlPlaneError, match="403"):
            request_json(base, "GET", "/tasks/other-task", **agent_headers)
        with pytest.raises(ControlPlaneError, match="403"):
            request_json(
                base,
                "POST",
                "/tasks/other-task/artifacts",
                payload={"name": "forged.txt", "content": "nope", "provenance": "worker-a"},
                **agent_headers,
            )
        uploaded = request_json(
            base,
            "POST",
            "/tasks/owned-task/artifacts",
            payload={
                "name": "owned.txt",
                "content": "allowed",
                "provenance": "worker-a",
                "lease_id": leased["lease"]["lease_id"],
            },
            **agent_headers,
        )
        assert uploaded["artifact"]["name"] == "owned.txt"
    finally:
        stop_server(server, thread)


def test_terminal_receipt_rejects_missing_or_mismatched_artifact(tmp_path) -> None:
    server, thread, base = run_server(tmp_path, token=None)
    try:
        request_json(base, "POST", "/tasks", payload={"task": task_payload("receipt-task")})
        leased = request_json(
            base,
            "POST",
            "/tasks/receipt-task/leases",
            payload={"worker_id": "worker-a", "ttl_seconds": 30},
        )
        lease_id = leased["lease"]["lease_id"]
        request_json(
            base,
            "POST",
            "/tasks/receipt-task/transition",
            payload={"state": "running", "actor": "worker-a", "lease_id": lease_id, "reason": "started"},
        )
        with pytest.raises(ControlPlaneError, match="422"):
            request_json(
                base,
                "POST",
                "/tasks/receipt-task/transition",
                payload={
                    "state": "succeeded",
                    "actor": "worker-a",
                    "lease_id": lease_id,
                    "reason": "forged receipt",
                    "evidence": {"verified": True},
                    "receipt": {
                        "protocol": "agent-relay/0.3",
                        "receipt_id": "forged-receipt",
                        "task_id": "receipt-task",
                        "final_state": "succeeded",
                        "actor": "worker-a",
                        "completed_at": "2026-08-23T00:00:00Z",
                        "evidence": {"verified": True},
                        "artifacts": [{
                            "artifact_id": "artifact-does-not-exist",
                            "name": "missing.patch",
                            "sha256": "0" * 64,
                            "size_bytes": 1,
                            "kind": "patch",
                            "media_type": "text/plain",
                            "provenance": "worker-a",
                            "uri": "/tasks/receipt-task/artifacts/artifact-does-not-exist",
                            "metadata": {},
                        }],
                        "verification": [],
                        "workspace": {},
                        "summary": "forged",
                    },
                },
            )
        assert request_json(base, "GET", "/tasks/receipt-task")["state"] == "running"
    finally:
        stop_server(server, thread)


def test_claim_next_returns_oldest_compatible_task(tmp_path) -> None:
    server, thread, base = run_server(tmp_path, token=None)
    try:
        request_json(
            base,
            "POST",
            "/agents/register",
            payload={
                "agent_id": "worker-a",
                "name": "Local worker",
                "readiness": "unknown",
                "capabilities": ["bounded-edit", "ollama"],
                "task_kinds": ["mechanical"],
                "transports": ["agent-relay-http"],
                "metadata": {"backend": "local-qwen"},
            },
        )
        request_json(
            base,
            "POST",
            "/tasks",
            payload={
                "task": task_payload("claude-only"),
                "workspace_policy": {"backend": "claude-task"},
            },
        )
        request_json(
            base,
            "POST",
            "/tasks",
            payload={
                "task": task_payload("local-task"),
                "workspace_policy": {"backend": "local-qwen"},
            },
        )

        claimed = request_json(
            base,
            "POST",
            "/tasks/claim",
            payload={"worker_id": "worker-a", "ttl_seconds": 30},
        )
        idle = request_json(
            base,
            "POST",
            "/tasks/claim",
            payload={"worker_id": "worker-a", "ttl_seconds": 30},
        )

        assert claimed["envelope"]["task"]["task_id"] == "local-task"
        assert claimed["lease"]["worker_id"] == "worker-a"
        assert idle["lease"] is None
        assert idle["reason"] == "no_compatible_work"
    finally:
        stop_server(server, thread)


def test_http_chain_step_is_gated_and_idempotent(tmp_path) -> None:
    server, thread, base = run_server(tmp_path, token=None)
    try:
        root = request_json(
            base,
            "POST",
            "/chains/chain-http/steps",
            payload={
                "step_id": "build",
                "step_index": 0,
                "task": task_payload("chain-http-parent"),
            },
        )
        assert root["created"] is True
        with pytest.raises(ControlPlaneError, match="409"):
            request_json(
                base,
                "POST",
                "/chains/chain-http/steps",
                payload={
                    "step_id": "review",
                    "step_index": 1,
                    "task": task_payload("chain-http-child"),
                    "predecessor_task_id": "chain-http-parent",
                },
            )

        leased = request_json(
            base,
            "POST",
            "/tasks/chain-http-parent/leases",
            payload={"worker_id": "worker-a", "ttl_seconds": 30},
        )
        lease_id = leased["lease"]["lease_id"]
        request_json(
            base,
            "POST",
            "/tasks/chain-http-parent/transition",
            payload={
                "state": "running",
                "actor": "worker-a",
                "lease_id": lease_id,
                "reason": "started",
            },
        )
        artifact = request_json(
            base,
            "POST",
            "/tasks/chain-http-parent/artifacts",
            payload={
                "name": "build.patch",
                "content": "bounded patch",
                "kind": "patch",
                "media_type": "text/plain",
                "provenance": "worker-a",
            },
        )["artifact"]
        request_json(
            base,
            "POST",
            "/tasks/chain-http-parent/transition",
            payload={
                "state": "succeeded",
                "actor": "worker-a",
                "lease_id": lease_id,
                "reason": "verified",
                "evidence": {"verified": True},
                "receipt": {
                    "protocol": "agent-relay/0.3",
                    "receipt_id": "receipt-chain-http-parent",
                    "task_id": "chain-http-parent",
                    "final_state": "succeeded",
                    "actor": "worker-a",
                    "completed_at": "2026-08-23T00:00:00Z",
                    "evidence": {"verified": True},
                    "artifacts": [],
                    "verification": [],
                    "workspace": {},
                    "summary": "verified",
                },
            },
        )
        child_payload = {
            "step_id": "review",
            "step_index": 1,
            "task": task_payload("chain-http-child"),
            "predecessor_task_id": "chain-http-parent",
            "parent_artifact_ids": [artifact["artifact_id"]],
            "parent_messages": ["Review only this patch."],
        }
        child = request_json(base, "POST", "/chains/chain-http/steps", payload=child_payload)
        duplicate = request_json(base, "POST", "/chains/chain-http/steps", payload=child_payload)
        chain = request_json(base, "GET", "/chains/chain-http")

        assert child["created"] is True
        assert duplicate["created"] is False
        assert child["envelope"]["parent_artifacts"][0]["artifact_id"] == artifact["artifact_id"]
        assert [step["step_id"] for step in chain["steps"]] == ["build", "review"]
    finally:
        stop_server(server, thread)


def test_http_deferred_chain_step_materializes_after_parent_terminal(tmp_path) -> None:
    server, thread, base = run_server(tmp_path, token=None)
    try:
        root = request_json(
            base,
            "POST",
            "/chains/chain-http-deferred/steps",
            payload={
                "step_id": "build",
                "step_index": 0,
                "task": task_payload("deferred-http-parent"),
            },
        )
        assert root["created"] is True
        scheduled = request_json(
            base,
            "POST",
            "/chains/chain-http-deferred/steps",
            payload={
                "step_id": "review",
                "step_index": 1,
                "task": task_payload("deferred-http-child"),
                "predecessor_task_id": "deferred-http-parent",
                "defer_until_ready": True,
                "parent_messages": ["Review only the parent result."],
            },
        )
        assert scheduled["pending"] is True
        assert scheduled["envelope"] is None
        before = request_json(base, "POST", "/chains/chain-http-deferred/reconcile", payload={})
        assert before["pending"] == 1

        leased = request_json(
            base,
            "POST",
            "/tasks/deferred-http-parent/leases",
            payload={"worker_id": "worker-a", "ttl_seconds": 30},
        )
        lease_id = leased["lease"]["lease_id"]
        request_json(
            base,
            "POST",
            "/tasks/deferred-http-parent/transition",
            payload={
                "state": "running",
                "actor": "worker-a",
                "lease_id": lease_id,
                "reason": "started",
            },
        )
        request_json(
            base,
            "POST",
            "/tasks/deferred-http-parent/transition",
            payload={
                "state": "succeeded",
                "actor": "worker-a",
                "lease_id": lease_id,
                "reason": "verified",
                "evidence": {"verified": True},
                "receipt": {
                    "protocol": "agent-relay/0.3",
                    "receipt_id": "receipt-deferred-http-parent",
                    "task_id": "deferred-http-parent",
                    "final_state": "succeeded",
                    "actor": "worker-a",
                    "completed_at": "2026-08-23T00:00:00Z",
                    "evidence": {"verified": True},
                    "artifacts": [],
                    "verification": [],
                    "workspace": {},
                    "summary": "verified",
                },
            },
        )
        child = request_json(base, "GET", "/tasks/deferred-http-child")
        chain = request_json(base, "GET", "/chains/chain-http-deferred")
        after = request_json(base, "POST", "/chains/chain-http-deferred/reconcile", payload={})
        assert child["state"] == "submitted"
        assert [step["step_id"] for step in chain["steps"]] == ["build", "review"]
        assert chain["pending_steps"][0]["status"] == "materialized"
        assert after["materialized"] == 1
    finally:
        stop_server(server, thread)
