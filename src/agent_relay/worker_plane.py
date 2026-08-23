"""Reference worker loop for the durable Agent Relay coordinator."""

from __future__ import annotations

import base64
from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import threading
import time
from typing import Any, Mapping

from .claude_task import run_claude_task
from .claude_mcp import ClaudeMCPConfig, run_claude_mcp_task
from .acceptance import enforce_acceptance
from .control import ControlPlaneError, request_json
from .protocol import (
    AgentCard,
    ArtifactRef,
    JobReceipt,
    JobState,
    Readiness,
    utc_now,
)
from .result import DelegationResult, ResultStatus
from .task import DelegationTask
from .delegate import delegate_local


@dataclass(frozen=True)
class WorkerConfig:
    coordinator_url: str = "http://127.0.0.1:8788"
    auth_token: str | None = None
    agent_token: str | None = None
    worker_id: str = "agent-relay-worker"
    repo: Path = Path(".")
    backend: str = "claude-task"
    model: str | None = None
    lease_seconds: float = 300.0
    poll_seconds: float = 2.0
    claim_next: bool = False

    def __post_init__(self) -> None:
        if self.backend not in {"local-qwen", "claude-task", "claude-mcp"}:
            raise ValueError("worker backend must be local-qwen, claude-task, or claude-mcp")
        if not self.worker_id.strip():
            raise ValueError("worker_id must not be empty")
        if self.lease_seconds <= 0 or self.poll_seconds <= 0:
            raise ValueError("lease and poll durations must be positive")


def _request(
    config: WorkerConfig,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"auth_token": config.auth_token}
    if config.agent_token:
        kwargs.update({"agent_id": config.worker_id, "agent_token": config.agent_token})
    return request_json(config.coordinator_url, method, path, payload=payload, **kwargs)


def worker_card(config: WorkerConfig, *, readiness: Readiness = Readiness.UNKNOWN) -> AgentCard:
    capabilities = ["lease-reporting"]
    if config.backend == "claude-mcp":
        capabilities.extend(["prompt-execution", "remote-mcp"])
    else:
        capabilities.extend(["bounded-edit", "parent-verification"])
    if config.backend == "claude-task":
        capabilities.append("claude-a2a")
    elif config.backend == "claude-mcp":
        capabilities.append("claude-mcp")
    else:
        capabilities.append("ollama")
    return AgentCard(
        agent_id=config.worker_id,
        name=f"Agent Relay {config.backend} worker",
        readiness=readiness,
        capabilities=tuple(capabilities),
        task_kinds=("mechanical", "bounded_bugfix", "documentation", "formatting", "test_generation"),
        transports=("agent-relay-http",),
        workspace={"repo": str(config.repo.resolve()), "os": os.name, "sandbox": "adapter-owned"},
        artifact_limits={"max_bytes": 2 * 1024 * 1024},
        metadata={"backend": config.backend, "probe": "registration-only"},
    )


def _execution_repo(config: WorkerConfig, envelope: dict[str, Any]) -> Path:
    """Resolve an optional task workdir without escaping the worker root."""

    policy = envelope.get("workspace_policy", {})
    if not isinstance(policy, dict):
        raise ControlPlaneError("workspace_policy must be an object")
    if config.backend == "claude-mcp":
        return config.repo.expanduser().resolve()
    requested = policy.get("workdir")
    root = config.repo.expanduser().resolve()
    if requested is None:
        return root
    if not isinstance(requested, str) or not requested.strip():
        raise ControlPlaneError("workspace_policy.workdir must be a non-empty path")
    candidate = Path(requested).expanduser().resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ControlPlaneError("task workdir is outside the worker repository") from exc
    if not candidate.is_dir():
        raise ControlPlaneError(f"task workdir is not an existing directory: {candidate}")
    return candidate


def _worker_can_claim(raw: dict[str, Any], config: WorkerConfig) -> bool:
    """Avoid claiming work that explicitly targets another worker capability."""

    policy = raw.get("workspace_policy")
    if not isinstance(policy, dict):
        return True
    required_backend = policy.get("backend")
    if isinstance(required_backend, str) and required_backend and required_backend != config.backend:
        return False
    required = policy.get("required_capabilities", [])
    if isinstance(required, str):
        required = [required]
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        return False
    available = set(worker_card(config).capabilities)
    if not set(required).issubset(available):
        return False
    try:
        _execution_repo(config, raw)
    except ControlPlaneError:
        return False
    return True


def _final_state(result: DelegationResult) -> JobState:
    if result.status is ResultStatus.SUCCESS:
        return JobState.SUCCEEDED
    if result.status in {ResultStatus.BLOCKED, ResultStatus.SCOPE_VIOLATION}:
        return JobState.BLOCKED
    return JobState.FAILED


def _recovery_attempts(envelope: dict[str, Any]) -> int:
    events = envelope.get("events", [])
    if not isinstance(events, list):
        return 0
    return sum(
        1
        for event in events
        if isinstance(event, dict)
        and isinstance(event.get("data"), dict)
        and event["data"].get("retryable_adapter_failure") is True
    )


def _retryable_adapter_failure(result: DelegationResult, task: DelegationTask, envelope: dict[str, Any]) -> tuple[bool, int]:
    if result.status is not ResultStatus.WORKER_ERROR:
        return False, 0
    if result.metadata.get("retryable") is not True:
        return False, 0
    attempt = _recovery_attempts(envelope) + 1
    return attempt <= task.retry_limit, attempt


def _receipt(
    result: DelegationResult,
    *,
    task_id: str,
    worker_id: str,
    repo: Path,
    final_state: JobState,
    artifacts: tuple[ArtifactRef, ...] = (),
    cancellation_requested: bool = False,
    execution_stopped: bool | None = None,
    parent_inputs: tuple[dict[str, Any], ...] = (),
) -> JobReceipt:
    handoff = result.to_handoff()
    evidence = {
        "result_status": result.status.value,
        "summary": result.summary[:2_000],
        "files_changed": list(result.files_changed),
        "attempts": result.attempts,
        "main_worktree_unchanged": result.metadata.get("main_worktree_unchanged"),
        "patch_sha256": handoff["patch"]["sha256"],
        "patch_bytes": handoff["patch"]["bytes"],
        "sandbox_mode": result.sandbox_mode,
    }
    if cancellation_requested:
        evidence["cancel_requested"] = True
        evidence["execution_stopped"] = execution_stopped is True
        evidence["cancellation_boundary"] = (
            "confirmed_stopped" if execution_stopped is True else "adapter_stop_not_proven"
        )
    if result.blockers:
        evidence["blockers"] = list(result.blockers)[:5]
    sol_review = result.metadata.get("sol_review")
    if isinstance(sol_review, Mapping):
        evidence["sol_review"] = dict(sol_review)
    acceptance_gates = result.metadata.get("acceptance_gates")
    if isinstance(acceptance_gates, list):
        evidence["acceptance_gates"] = list(acceptance_gates)
    for key in (
        "lane",
        "transport",
        "remote_endpoint",
        "remote_host",
        "remote_workdir",
        "verification_authority",
        "execution_stopped",
    ):
        value = result.metadata.get(key)
        if isinstance(value, (str, bool)):
            evidence[key] = value
    if parent_inputs:
        evidence["parent_inputs"] = [dict(item) for item in parent_inputs[:16]]
    return JobReceipt(
        receipt_id=f"receipt_{task_id}_{worker_id}_{int(time.time() * 1000)}",
        task_id=task_id,
        final_state=final_state,
        actor=worker_id,
        completed_at=utc_now(),
        evidence=evidence,
        artifacts=artifacts,
        verification=tuple(item.to_dict() for item in result.verification),
        workspace={"repo": str(repo.resolve()), "sandbox_mode": result.sandbox_mode},
        summary=result.summary[:2_000],
    )


def execute_task(
    config: WorkerConfig,
    task: DelegationTask,
    *,
    cancel_event: threading.Event | None = None,
    workspace_policy: Mapping[str, Any] | None = None,
) -> DelegationResult:
    if config.backend == "claude-task":
        return run_claude_task(task, config.repo, cancel_event=cancel_event)
    if config.backend == "claude-mcp":
        selected = ClaudeMCPConfig.from_env()
        policy = workspace_policy or {}
        requested_workdir = policy.get("mcp_workdir", policy.get("workdir"))
        if isinstance(requested_workdir, str) and requested_workdir.strip():
            selected = replace(selected, workdir=requested_workdir.strip())
        return run_claude_mcp_task(task, config=selected, cancel_event=cancel_event)
    return delegate_local(task=task, repo=config.repo, model=config.model)


def _load_parent_inputs(
    config: WorkerConfig,
    envelope: dict[str, Any],
) -> tuple[DelegationTask, tuple[dict[str, Any], ...]]:
    """Fetch only the child envelope's declared parent inputs.

    The coordinator authorizes these reads because the child is leased to this
    worker and the artifact references are explicit in the child envelope.
    Content is bounded before it enters an adapter prompt; the hash and size
    are recorded in the terminal receipt.
    """

    raw_task = envelope.get("task", {})
    task = DelegationTask.from_dict(raw_task)
    refs = envelope.get("parent_artifacts", [])
    messages = envelope.get("parent_messages", [])
    if not isinstance(refs, list) or not isinstance(messages, list):
        raise ControlPlaneError("child envelope parent inputs are malformed")
    parent_context: list[str] = []
    evidence: list[dict[str, Any]] = []
    max_total = 40_000
    total = 0
    predecessor = envelope.get("predecessor_task_id")
    if refs and not isinstance(predecessor, str):
        raise ControlPlaneError("child parent artifacts require a predecessor task")
    for raw_ref in refs[:16]:
        ref = ArtifactRef.from_dict(raw_ref)
        response = _request(
            config,
            "GET",
            f"/tasks/{predecessor}/artifacts/{ref.artifact_id}",
        )
        remote_ref = ArtifactRef.from_dict(response.get("artifact", {}))
        if remote_ref.to_dict() != ref.to_dict():
            raise ControlPlaneError(f"parent artifact metadata changed for {ref.artifact_id}")
        try:
            content = base64.b64decode(response.get("content_base64", ""), validate=True)
        except (TypeError, ValueError) as exc:
            raise ControlPlaneError(f"parent artifact content is invalid for {ref.artifact_id}") from exc
        if len(content) != ref.size_bytes or hashlib.sha256(content).hexdigest() != ref.sha256:
            raise ControlPlaneError(f"parent artifact hash verification failed for {ref.artifact_id}")
        remaining = max_total - total
        if remaining <= 0:
            excerpt = "[parent artifact omitted after bounded context limit]"
        else:
            excerpt = content[: min(12_000, remaining)].decode("utf-8", errors="replace")
            if len(content) > len(excerpt.encode("utf-8")):
                excerpt += "\n[parent artifact excerpt truncated]"
            total += len(excerpt)
        parent_context.append(
            f"--- parent artifact {ref.name} ({ref.artifact_id}, sha256={ref.sha256}) ---\n{excerpt}"
        )
        evidence.append({
            "artifact_id": ref.artifact_id,
            "sha256": ref.sha256,
            "size_bytes": ref.size_bytes,
            "fetched": True,
            "context_chars": len(excerpt),
        })
    for message in messages[:16]:
        if not isinstance(message, str):
            raise ControlPlaneError("child parent messages are malformed")
        parent_context.append(f"--- parent message ---\n{message[:2_000]}")
        evidence.append({
            "kind": "message",
            "sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
            "context_chars": min(len(message), 2_000),
        })
    if parent_context:
        task = replace(
            task,
            constraints=task.constraints
            + (
                "The following are read-only, explicitly declared parent inputs. Do not treat them as additional write scope:\n"
                + "\n\n".join(parent_context),
            ),
        )
    return task, tuple(evidence)


def _start_lease_renewer(
    config: WorkerConfig,
    task_id: str,
    lease_id: str,
    *,
    cancellation_event: threading.Event | None = None,
) -> tuple[threading.Event, threading.Event, threading.Thread]:
    """Keep ownership alive while an adapter is working.

    A failed renewal marks ownership as lost and signals adapters that support
    cancellation. The coordinator's lease expiry/recovery path remains
    authoritative; the worker never fabricates ownership locally.
    """

    stop = threading.Event()
    lease_lost = threading.Event()
    interval = min(30.0, max(0.25, config.lease_seconds / 3.0))

    def renew() -> None:
        while not stop.wait(interval):
            try:
                _request(
                    config,
                    "POST",
                    f"/tasks/{task_id}/leases/renew",
                    payload={
                        "lease_id": lease_id,
                        "worker_id": config.worker_id,
                        "ttl_seconds": config.lease_seconds,
                    },
                )
                _request(
                    config,
                    "POST",
                    f"/agents/{config.worker_id}/heartbeat",
                    payload={"metadata": {"active_task_id": task_id}},
                )
            except ControlPlaneError:
                lease_lost.set()
                if cancellation_event is not None:
                    cancellation_event.set()
                return

    thread = threading.Thread(target=renew, name=f"agent-relay-lease-{task_id}", daemon=True)
    thread.start()
    return stop, lease_lost, thread


def _start_cancel_monitor(config: WorkerConfig, task_id: str) -> tuple[threading.Event, threading.Event, threading.Thread]:
    """Watch durable state and signal adapters that support stopping."""

    stop = threading.Event()
    cancelled = threading.Event()
    interval = min(1.0, max(0.2, config.poll_seconds))

    def monitor() -> None:
        while not stop.wait(interval):
            try:
                current = _request(config, "GET", f"/tasks/{task_id}")
            except ControlPlaneError:
                continue
            if current.get("state") == JobState.CANCEL_REQUESTED.value:
                cancelled.set()
                return

    thread = threading.Thread(target=monitor, name=f"agent-relay-cancel-{task_id}", daemon=True)
    thread.start()
    return stop, cancelled, thread


def run_worker_once(config: WorkerConfig) -> list[dict[str, Any]]:
    """Claim and execute each currently claimable task once.

    A worker can safely run this function from a scheduler or use
    :func:`run_worker_forever`. Claim conflicts are expected when multiple
    workers poll the same coordinator and are returned as skipped items.
    """

    card = worker_card(config)
    _request(
        config,
        "POST",
        "/agents/register",
        payload=card.to_dict(),
    )
    if config.claim_next:
        scheduled = _request(
            config,
            "POST",
            "/tasks/claim",
            payload={"worker_id": config.worker_id, "ttl_seconds": config.lease_seconds},
        )
        listing = {"tasks": [scheduled["envelope"]]} if scheduled.get("lease") else {"tasks": []}
    else:
        listing = _request(
            config,
            "GET",
            "/tasks",
        )
    outcomes: list[dict[str, Any]] = []
    for raw in listing.get("tasks", []):
        if not isinstance(raw, dict):
            continue
        state = raw.get("state")
        # Include running tasks: acquire_lease will reject a live owner but
        # will explicitly move an expired owner to waiting before reassigning.
        if state not in {
            JobState.SUBMITTED.value,
            JobState.ACCEPTED.value,
            JobState.WAITING.value,
            JobState.RUNNING.value,
        }:
            continue
        if not _worker_can_claim(raw, config):
            continue
        task_id = raw.get("task", {}).get("task_id") or raw.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        lease_id: str | None = None
        result: DelegationResult | None = None
        execution_repo = config.repo
        try:
            leased = _request(
                config,
                "POST",
                f"/tasks/{task_id}/leases",
                payload={"worker_id": config.worker_id, "ttl_seconds": config.lease_seconds},
            )
            lease = leased["lease"]
            lease_id = lease["lease_id"]
            execution_repo = _execution_repo(config, leased["envelope"])
            _request(
                config,
                "POST",
                f"/tasks/{task_id}/transition",
                payload={
                    "state": JobState.RUNNING.value,
                    "actor": config.worker_id,
                    "lease_id": lease_id,
                    "reason": "worker started execution",
                },
            )
            task, parent_inputs = _load_parent_inputs(config, leased["envelope"])
            cancel_stop, cancel_event, cancel_thread = _start_cancel_monitor(config, task_id)
            renew_stop, lease_lost, renew_thread = _start_lease_renewer(
                config,
                task_id,
                lease_id,
                cancellation_event=cancel_event,
            )
            try:
                execute_kwargs: dict[str, Any] = {"cancel_event": cancel_event}
                if config.backend == "claude-mcp":
                    execute_kwargs["workspace_policy"] = leased["envelope"].get("workspace_policy", {})
                result = execute_task(replace(config, repo=execution_repo), task, **execute_kwargs)
            finally:
                renew_stop.set()
                renew_thread.join(timeout=min(2.0, max(0.1, config.lease_seconds / 4.0)))
                cancel_stop.set()
                cancel_thread.join(timeout=min(2.0, max(0.1, config.poll_seconds * 2.0)))
            if lease_lost.is_set():
                outcomes.append(
                    {
                        "task_id": task_id,
                        "status": "lease_lost",
                        "execution_stopped": bool(result and result.metadata.get("execution_stopped") is True),
                        "coordinator_recovery": "lease_expiry_or_reassignment",
                    }
                )
                continue
            if config.backend == "claude-task":
                result = enforce_acceptance(
                    task,
                    execution_repo,
                    result,
                    require_sol_review=True,
                )
            retryable, recovery_attempt = _retryable_adapter_failure(result, task, leased["envelope"])
            if retryable:
                _request(
                    config,
                    "POST",
                    f"/tasks/{task_id}/transition",
                    payload={
                        "state": JobState.WAITING.value,
                        "actor": config.worker_id,
                        "lease_id": lease_id,
                        "reason": "retryable adapter failure; task returned to waiting",
                        "data": {
                            "retryable_adapter_failure": True,
                            "recovery_attempt": recovery_attempt,
                            "failure_kind": result.metadata.get("failure_kind", "adapter"),
                            "error": str(result.metadata.get("adapter_error", result.summary))[:2_000],
                        },
                    },
                )
                try:
                    _request(
                        config,
                        "POST",
                        f"/tasks/{task_id}/leases/release",
                        payload={"lease_id": lease_id, "worker_id": config.worker_id},
                    )
                except ControlPlaneError:
                    # The waiting transition is already durable. Lease expiry
                    # remains the safe fallback if release reporting fails.
                    pass
                outcomes.append(
                    {
                        "task_id": task_id,
                        "status": JobState.WAITING.value,
                        "retryable": True,
                        "recovery_attempt": recovery_attempt,
                    }
                )
                continue
            final_state = _final_state(result)
            current = _request(config, "GET", f"/tasks/{task_id}")
            cancellation_requested = current.get("state") == JobState.CANCEL_REQUESTED.value
            execution_stopped = result.metadata.get("execution_stopped") is True
            if cancellation_requested:
                final_state = JobState.CANCELLED if execution_stopped else JobState.BLOCKED
            _request(
                config,
                "POST",
                f"/agents/{config.worker_id}/heartbeat",
                payload={
                    "readiness": Readiness.READY.value if result.success else Readiness.DEGRADED.value,
                    "metadata": {"last_task_id": task_id, "last_task_state": final_state.value},
                },
            )
            artifacts: tuple[ArtifactRef, ...] = ()
            if not cancellation_requested and result.patch and len(result.patch.encode("utf-8")) <= 2 * 1024 * 1024:
                artifact_response = _request(
                    config,
                    "POST",
                    f"/tasks/{task_id}/artifacts",
                    payload={
                        "name": f"{task_id}.patch",
                        "content": result.patch,
                        "kind": "patch",
                        "media_type": "text/x-diff",
                        "provenance": config.worker_id,
                        "lease_id": lease_id,
                    },
                )
                artifacts = (ArtifactRef.from_dict(artifact_response["artifact"]),)
            receipt = _receipt(
                result,
                task_id=task_id,
                worker_id=config.worker_id,
                repo=execution_repo,
                final_state=final_state,
                artifacts=artifacts,
                cancellation_requested=cancellation_requested,
                execution_stopped=execution_stopped if cancellation_requested else None,
                parent_inputs=parent_inputs,
            )
            terminal = _request(
                config,
                "POST",
                f"/tasks/{task_id}/transition",
                payload={
                    "state": final_state.value,
                    "actor": config.worker_id,
                    "lease_id": lease_id,
                    "reason": "worker returned terminal result",
                    "evidence": dict(receipt.evidence),
                    "receipt": receipt.to_dict(),
                },
            )
            outcomes.append({"task_id": task_id, "status": final_state.value, "result": terminal})
        except ControlPlaneError as exc:
            if lease_id and result is not None:
                try:
                    current = _request(config, "GET", f"/tasks/{task_id}")
                except ControlPlaneError:
                    current = {}
                if current.get("state") == JobState.CANCEL_REQUESTED.value:
                    blocked_receipt = _receipt(
                        result,
                        task_id=task_id,
                        worker_id=config.worker_id,
                        repo=execution_repo,
                        final_state=JobState.BLOCKED,
                        cancellation_requested=True,
                        execution_stopped=False,
                    )
                    try:
                        terminal = _request(
                            config,
                            "POST",
                            f"/tasks/{task_id}/transition",
                            payload={
                                "state": JobState.BLOCKED.value,
                                "actor": config.worker_id,
                                "lease_id": lease_id,
                                "reason": "cancellation was requested but adapter stop was not proven",
                                "evidence": dict(blocked_receipt.evidence),
                                "receipt": blocked_receipt.to_dict(),
                            },
                        )
                        outcomes.append({"task_id": task_id, "status": JobState.BLOCKED.value, "result": terminal})
                        continue
                    except ControlPlaneError:
                        pass
            outcomes.append({"task_id": task_id, "status": "skipped", "error": str(exc)})
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            if lease_id:
                failure_receipt = JobReceipt(
                    receipt_id=f"receipt_{task_id}_{config.worker_id}_{int(time.time() * 1000)}",
                    task_id=task_id,
                    final_state=JobState.FAILED,
                    actor=config.worker_id,
                    completed_at=utc_now(),
                    evidence={"worker_error": detail, "execution_stopped": False},
                    summary="worker failed before producing a normal adapter result",
                )
                try:
                    _request(
                        config,
                        "POST",
                        f"/tasks/{task_id}/transition",
                        payload={
                            "state": JobState.FAILED.value,
                            "actor": config.worker_id,
                            "lease_id": lease_id,
                            "reason": "worker failed during execution",
                            "evidence": dict(failure_receipt.evidence),
                            "receipt": failure_receipt.to_dict(),
                        },
                    )
                    outcomes.append({"task_id": task_id, "status": JobState.FAILED.value, "error": detail})
                    continue
                except ControlPlaneError as report_error:
                    detail += f"; could not report failure: {report_error}"
            outcomes.append({"task_id": task_id, "status": "worker_error", "error": detail})
    return outcomes


def run_worker_forever(config: WorkerConfig) -> None:
    while True:
        try:
            run_worker_once(config)
        except ControlPlaneError:
            pass
        time.sleep(config.poll_seconds)
