from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import threading
import time

from agent_relay.protocol import ArtifactRef, JobEnvelope, JobState
from agent_relay.result import DelegationResult, ResultStatus, VerificationResult
from agent_relay.task import DelegationTask
import agent_relay.worker_plane as worker_plane


def task(task_id: str = "worker-plane-task") -> DelegationTask:
    return DelegationTask(
        task_id=task_id,
        objective="Complete one worker-plane task.",
        allowed_files=("value.py",),
        verification=("python -c \"assert True\"",),
        task_kind="mechanical",
    )


def test_worker_claims_executes_and_reports_terminal_receipt(monkeypatch, tmp_path: Path) -> None:
    submitted = JobEnvelope.new(task())
    accepted = submitted.transition(
        JobState.ACCEPTED,
        actor="coordinator",
        reason="worker lease accepted",
    )
    calls = []

    def fake_request(base_url, method, path, *, payload=None, auth_token=None, timeout=10):
        calls.append((method, path, payload))
        if method == "POST" and path == "/agents/register":
            return {"agent": payload}
        if method == "GET" and path == "/tasks":
            return {"tasks": [submitted.to_dict()]}
        if method == "GET" and path == "/tasks/worker-plane-task":
            return {"state": "running"}
        if method == "POST" and path == "/agents/worker-a/heartbeat":
            return {"agent": {"agent_id": "worker-a", "readiness": payload["readiness"]}}
        if method == "POST" and path == "/tasks/worker-plane-task/leases":
            return {
                "envelope": accepted.to_dict(),
                "lease": {
                    "task_id": "worker-plane-task",
                    "lease_id": "lease-1",
                    "worker_id": "worker-a",
                    "expires_at": "2099-01-01T00:00:00Z",
                },
            }
        if method == "POST" and path.endswith("/transition"):
            if payload["state"] == "running":
                return {"state": "running"}
            assert payload["state"] == "succeeded"
            assert payload["receipt"]["final_state"] == "succeeded"
            assert payload["evidence"]["result_status"] == "SUCCESS"
            return {"state": "succeeded", "receipt": payload["receipt"]}
        raise AssertionError((method, path, payload))

    monkeypatch.setattr(worker_plane, "request_json", fake_request)
    monkeypatch.setattr(
        worker_plane,
        "execute_task",
        lambda _config, _task, **_kwargs: DelegationResult(
            task_id="worker-plane-task",
            status=ResultStatus.SUCCESS,
            summary="verified",
            verification=(VerificationResult("python -c assert True", 0),),
            metadata={"main_worktree_unchanged": True},
        ),
    )

    outcomes = worker_plane.run_worker_once(
        worker_plane.WorkerConfig(
            coordinator_url="http://coordinator",
            worker_id="worker-a",
            repo=tmp_path,
            backend="local-qwen",
        )
    )

    assert outcomes[0]["status"] == "succeeded"
    assert [path for _method, path, _payload in calls] == [
        "/agents/register",
        "/tasks",
        "/tasks/worker-plane-task/leases",
        "/tasks/worker-plane-task/transition",
        "/tasks/worker-plane-task",
        "/agents/worker-a/heartbeat",
        "/tasks/worker-plane-task/transition",
    ]


def test_worker_fetches_only_declared_parent_artifact_inputs(monkeypatch, tmp_path: Path) -> None:
    content = b"diff --git a/value.py b/value.py\n+VALUE = 2\n"
    ref = ArtifactRef(
        artifact_id="artifact-parent-patch",
        name="parent.patch",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        kind="patch",
        media_type="text/x-diff",
        provenance="worker-parent",
        uri="/tasks/parent-task/artifacts/artifact-parent-patch",
        metadata={},
    )
    child = JobEnvelope.new(
        task("child-task"),
        chain_id="chain-parent-input",
        chain_step_id="review",
        chain_step_index=1,
        predecessor_task_id="parent-task",
        parent_artifacts=(ref,),
        parent_messages=("Review the declared patch only.",),
    )
    accepted = child.transition(JobState.ACCEPTED, actor="coordinator", reason="leased")
    captured: list[DelegationTask] = []

    def fake_request(base_url, method, path, *, payload=None, auth_token=None, timeout=10):
        if path == "/agents/register":
            return {"agent": payload}
        if path == "/tasks":
            return {"tasks": [child.to_dict()]}
        if path == "/tasks/child-task/leases":
            return {
                "envelope": accepted.to_dict(),
                "lease": {
                    "task_id": "child-task",
                    "lease_id": "lease-child",
                    "worker_id": "worker-a",
                    "expires_at": "2099-01-01T00:00:00Z",
                },
            }
        if path == "/tasks/parent-task/artifacts/artifact-parent-patch":
            return {"artifact": ref.to_dict(), "content_base64": base64.b64encode(content).decode("ascii")}
        if path == "/tasks/child-task":
            return {"state": "running"}
        if path == "/agents/worker-a/heartbeat":
            return {"agent": payload}
        if path == "/tasks/child-task/transition":
            if payload["state"] == "running":
                return {"state": "running"}
            assert payload["receipt"]["evidence"]["parent_inputs"][0]["artifact_id"] == ref.artifact_id
            return {"state": "succeeded", "receipt": payload["receipt"]}
        raise AssertionError((method, path, payload))

    monkeypatch.setattr(worker_plane, "request_json", fake_request)

    def fake_execute(_config, execution_task, **_kwargs):
        captured.append(execution_task)
        return DelegationResult(
            task_id="child-task",
            status=ResultStatus.SUCCESS,
            summary="reviewed",
            metadata={"main_worktree_unchanged": True},
        )

    monkeypatch.setattr(worker_plane, "execute_task", fake_execute)
    outcomes = worker_plane.run_worker_once(
        worker_plane.WorkerConfig(
            coordinator_url="http://coordinator",
            worker_id="worker-a",
            repo=tmp_path,
            backend="local-qwen",
        )
    )

    assert outcomes[0]["status"] == "succeeded"
    assert len(captured) == 1
    assert "VALUE = 2" in captured[0].constraints[-1]
    assert "Review the declared patch only." in captured[0].constraints[-1]


def test_worker_renews_lease_and_agent_heartbeat(monkeypatch, tmp_path: Path) -> None:
    calls = []

    def fake_request(base_url, method, path, *, payload=None, auth_token=None, timeout=10):
        calls.append((method, path, payload))
        return {}

    monkeypatch.setattr(worker_plane, "request_json", fake_request)
    config = worker_plane.WorkerConfig(
        coordinator_url="http://coordinator",
        worker_id="worker-a",
        repo=tmp_path,
        backend="local-qwen",
        lease_seconds=0.3,
    )
    stop, lease_lost, thread = worker_plane._start_lease_renewer(config, "task-1", "lease-1")
    time.sleep(0.35)
    stop.set()
    thread.join(timeout=1)

    assert any(path == "/tasks/task-1/leases/renew" for _method, path, _payload in calls)
    assert any(path == "/agents/worker-a/heartbeat" for _method, path, _payload in calls)
    assert lease_lost.is_set() is False


def test_lease_renewal_loss_signals_adapter_cancellation(monkeypatch, tmp_path: Path) -> None:
    calls = []

    def fake_request(base_url, method, path, *, payload=None, auth_token=None, timeout=10):
        calls.append(path)
        if path.endswith("/leases/renew"):
            raise worker_plane.ControlPlaneError("coordinator unavailable")
        return {}

    monkeypatch.setattr(worker_plane, "request_json", fake_request)
    cancellation = threading.Event()
    config = worker_plane.WorkerConfig(
        coordinator_url="http://coordinator",
        worker_id="worker-a",
        repo=tmp_path,
        backend="claude-task",
        lease_seconds=0.3,
    )
    stop, lease_lost, thread = worker_plane._start_lease_renewer(
        config,
        "task-1",
        "lease-1",
        cancellation_event=cancellation,
    )
    thread.join(timeout=1)
    stop.set()
    assert lease_lost.is_set() is True
    assert cancellation.is_set() is True


def test_worker_does_not_report_success_after_lease_loss(monkeypatch, tmp_path: Path) -> None:
    submitted = JobEnvelope.new(task("lease-loss-task"))
    accepted = submitted.transition(JobState.ACCEPTED, actor="coordinator", reason="accepted")
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(base_url, method, path, *, payload=None, auth_token=None, timeout=10):
        calls.append((method, path, payload))
        if path == "/agents/register":
            return {"agent": payload}
        if path == "/tasks":
            return {"tasks": [submitted.to_dict()]}
        if path == "/tasks/lease-loss-task/leases":
            return {
                "envelope": accepted.to_dict(),
                "lease": {
                    "task_id": "lease-loss-task",
                    "lease_id": "lease-loss",
                    "worker_id": "worker-a",
                    "expires_at": "2099-01-01T00:00:00Z",
                },
            }
        if path == "/tasks/lease-loss-task/transition":
            assert payload is not None
            assert payload["state"] == "running"
            return {"state": "running"}
        if path == "/tasks/lease-loss-task":
            return {"state": "running"}
        if path.endswith("/leases/renew"):
            raise worker_plane.ControlPlaneError("coordinator unavailable")
        return {}

    monkeypatch.setattr(worker_plane, "request_json", fake_request)

    def fake_execute(_config, _task, *, cancel_event=None):
        assert cancel_event is not None
        cancel_event.wait(timeout=1)
        return DelegationResult(
            task_id="lease-loss-task",
            status=ResultStatus.SUCCESS,
            summary="adapter would have succeeded",
            metadata={"execution_stopped": True},
        )

    monkeypatch.setattr(worker_plane, "execute_task", fake_execute)
    outcomes = worker_plane.run_worker_once(
        worker_plane.WorkerConfig(
            coordinator_url="http://coordinator",
            worker_id="worker-a",
            repo=tmp_path,
            backend="claude-task",
            lease_seconds=0.3,
        )
    )

    assert outcomes[0]["status"] == "lease_lost"
    assert not any(
        path == "/tasks/lease-loss-task/transition" and payload and payload.get("state") != "running"
        for _method, path, payload in calls
    )


def test_worker_considers_running_tasks_for_expired_lease_recovery(monkeypatch, tmp_path: Path) -> None:
    submitted = JobEnvelope.new(task("recoverable-task")).transition(
        JobState.ACCEPTED,
        actor="coordinator",
        reason="old worker accepted",
    ).transition(
        JobState.RUNNING,
        actor="worker-old",
        reason="old worker started",
    )
    calls = []

    def fake_request(base_url, method, path, *, payload=None, auth_token=None, timeout=10):
        calls.append((method, path, payload))
        if path == "/agents/register":
            return {"agent": payload}
        if path == "/tasks":
            return {"tasks": [submitted.to_dict()]}
        if path.endswith("/leases"):
            return {
                "envelope": submitted.to_dict(),
                "lease": {"task_id": "recoverable-task", "lease_id": "lease-new", "worker_id": "worker-a", "expires_at": "2099-01-01T00:00:00Z"},
            }
        raise worker_plane.ControlPlaneError("live lease")

    monkeypatch.setattr(worker_plane, "request_json", fake_request)
    monkeypatch.setattr(worker_plane, "execute_task", lambda _config, _task, **_kwargs: (_ for _ in ()).throw(AssertionError("should not execute in this filter test")))

    outcomes = worker_plane.run_worker_once(
        worker_plane.WorkerConfig(coordinator_url="http://coordinator", worker_id="worker-a", repo=tmp_path)
    )

    assert outcomes[0]["task_id"] == "recoverable-task"
    assert any(path == "/tasks/recoverable-task/leases" for _method, path, _payload in calls)


def test_worker_does_not_claim_success_after_unstoppable_cancel(monkeypatch, tmp_path: Path) -> None:
    submitted = JobEnvelope.new(task("cancel-boundary-task"))
    accepted = submitted.transition(JobState.ACCEPTED, actor="coordinator", reason="accepted")
    calls = []

    def fake_request(base_url, method, path, *, payload=None, auth_token=None, timeout=10):
        calls.append((method, path, payload))
        if path == "/agents/register":
            return {"agent": payload}
        if path == "/tasks":
            return {"tasks": [submitted.to_dict()]}
        if path == "/tasks/cancel-boundary-task/leases":
            return {"envelope": accepted.to_dict(), "lease": {"lease_id": "lease-cancel", "worker_id": "worker-a"}}
        if path == "/tasks/cancel-boundary-task/transition" and payload["state"] == "running":
            return {"state": "running"}
        if path == "/tasks/cancel-boundary-task":
            return {"state": "cancel_requested"}
        if path == "/agents/worker-a/heartbeat":
            return {"agent": payload}
        if path == "/tasks/cancel-boundary-task/transition":
            assert payload["state"] == "blocked"
            assert payload["receipt"]["final_state"] == "blocked"
            assert payload["evidence"]["cancel_requested"] is True
            assert payload["evidence"]["execution_stopped"] is False
            return {"state": "blocked", "receipt": payload["receipt"]}
        raise AssertionError((method, path, payload))

    monkeypatch.setattr(worker_plane, "request_json", fake_request)
    monkeypatch.setattr(
        worker_plane,
        "execute_task",
        lambda _config, _task, **_kwargs: DelegationResult(
            task_id="cancel-boundary-task",
            status=ResultStatus.SUCCESS,
            summary="adapter returned after cancellation request",
            metadata={"main_worktree_unchanged": True},
        ),
    )

    outcomes = worker_plane.run_worker_once(
        worker_plane.WorkerConfig(coordinator_url="http://coordinator", worker_id="worker-a", repo=tmp_path)
    )

    assert outcomes[0]["status"] == "blocked"


def test_worker_requeues_retryable_adapter_failure(monkeypatch, tmp_path: Path) -> None:
    submitted = JobEnvelope.new(task("retryable-adapter-task"))
    accepted = submitted.transition(JobState.ACCEPTED, actor="coordinator", reason="accepted")
    calls = []

    def fake_request(base_url, method, path, *, payload=None, auth_token=None, timeout=10):
        calls.append((method, path, payload))
        if path == "/agents/register":
            return {"agent": payload}
        if path == "/tasks":
            return {"tasks": [submitted.to_dict()]}
        if path == "/tasks/retryable-adapter-task/leases":
            return {
                "envelope": accepted.to_dict(),
                "lease": {"lease_id": "lease-retry", "worker_id": "worker-a"},
            }
        if path == "/tasks/retryable-adapter-task" and method == "GET":
            return {"state": "running"}
        if path == "/tasks/retryable-adapter-task/transition":
            if payload["state"] == "running":
                return {"state": "running"}
            assert payload["state"] == "waiting"
            assert payload["data"]["retryable_adapter_failure"] is True
            return {"state": "waiting"}
        if path == "/tasks/retryable-adapter-task/leases/release":
            assert payload["lease_id"] == "lease-retry"
            return {"state": "waiting", "lease_id": None}
        raise AssertionError((method, path, payload))

    monkeypatch.setattr(worker_plane, "request_json", fake_request)
    monkeypatch.setattr(
        worker_plane,
        "execute_task",
        lambda _config, _task, **_kwargs: DelegationResult(
            task_id="retryable-adapter-task",
            status=ResultStatus.WORKER_ERROR,
            summary="bridge timed out",
            metadata={
                "main_worktree_unchanged": True,
                "retryable": True,
                "failure_kind": "bridge_transport",
                "adapter_error": "timed out",
            },
        ),
    )

    outcomes = worker_plane.run_worker_once(
        worker_plane.WorkerConfig(
            coordinator_url="http://coordinator",
            worker_id="worker-a",
            repo=tmp_path,
            backend="claude-task",
        )
    )

    assert outcomes[0]["status"] == "waiting"
    assert outcomes[0]["retryable"] is True
    assert any(path.endswith("/leases/release") for _method, path, _payload in calls)


def test_worker_does_not_claim_task_for_another_backend(monkeypatch, tmp_path: Path) -> None:
    submitted = JobEnvelope.new(task("backend-filter-task"))
    raw = submitted.to_dict()
    raw["workspace_policy"] = {
        "backend": "local-qwen",
        "required_capabilities": ["ollama"],
    }
    calls = []

    def fake_request(_base_url, method, path, *, payload=None, auth_token=None, timeout=10):
        calls.append((method, path, payload))
        if path == "/agents/register":
            return {"agent": payload}
        if path == "/tasks":
            return {"tasks": [raw]}
        raise AssertionError((method, path, payload))

    monkeypatch.setattr(worker_plane, "request_json", fake_request)
    monkeypatch.setattr(
        worker_plane,
        "execute_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("wrong backend was claimed")),
    )

    outcomes = worker_plane.run_worker_once(
        worker_plane.WorkerConfig(
            coordinator_url="http://coordinator",
            worker_id="worker-claude",
            repo=tmp_path,
            backend="claude-task",
        )
    )

    assert outcomes == []
    assert not any(path.endswith("/leases") for _method, path, _payload in calls)
