from __future__ import annotations

import json
import time
from dataclasses import replace
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from agent_relay.agent_invocation import AgentInvocationConfig, AgentInvocationResult
from agent_relay.control import create_server
from agent_relay.mcp import _task_from_arguments, create_mcp_server
from agent_relay.result import DelegationResult, ResultStatus, VerificationResult
from agent_relay.worker_plane import WorkerConfig
import agent_relay.worker_plane as worker_plane


def task_payload(task_id: str = "mcp-task") -> dict:
    return {
        "task_id": task_id,
        "objective": "Run one bounded MCP-submitted task.",
        "allowed_files": ["value.py"],
        "verification": ["python -c \"assert True\""],
        "task_kind": "mechanical",
    }


def start_servers(
    tmp_path,
    *,
    local_worker: WorkerConfig | None = None,
    agent_invoker=None,
):
    coordinator = create_server(
        host="127.0.0.1",
        port=0,
        database=tmp_path / "relay.sqlite3",
        auth_token="coordinator-secret",
    )
    coordinator_thread = Thread(target=coordinator.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    coordinator_thread.start()
    coordinator_url = f"http://127.0.0.1:{coordinator.server_address[1]}"
    mcp = create_mcp_server(
        host="127.0.0.1",
        port=0,
        coordinator_url=coordinator_url,
        coordinator_token="coordinator-secret",
        auth_token="mcp-secret",
        max_workers=2,
        local_worker=local_worker,
        agent_invoker=agent_invoker,
    )
    mcp_thread = Thread(target=mcp.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    mcp_thread.start()
    mcp_url = f"http://127.0.0.1:{mcp.server_address[1]}/mcp"
    return coordinator, coordinator_thread, mcp, mcp_thread, mcp_url


def stop_servers(coordinator, coordinator_thread, mcp, mcp_thread) -> None:
    mcp.shutdown()
    mcp.server_close()
    mcp_thread.join(timeout=2)
    coordinator.shutdown()
    coordinator.server_close()
    coordinator_thread.join(timeout=2)


def post(url: str, payload: dict, *, token: str = "mcp-secret", session_id: str | None = None) -> tuple[int, dict[str, str], dict | None]:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    if session_id:
        headers["MCP-Session-Id"] = session_id
    request = Request(url, method="POST", data=json.dumps(payload).encode("utf-8"), headers=headers)
    try:
        with urlopen(request, timeout=5) as response:
            raw = response.read()
            return response.status, dict(response.headers.items()), json.loads(raw.decode("utf-8")) if raw else None
    except HTTPError as exc:
        raw = exc.read()
        return exc.code, dict(exc.headers.items()), json.loads(raw.decode("utf-8")) if raw else None


def initialize(url: str) -> str:
    status, headers, body = post(
        url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
        },
    )
    assert status == 200
    assert body["result"]["serverInfo"]["name"] == "agent-relay-mcp"
    session_id = headers.get("MCP-Session-Id")
    assert session_id
    status, _, _ = post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session_id=session_id)
    assert status == 204
    return session_id


def call(url: str, session_id: str, name: str, arguments: dict) -> dict:
    status, _, body = post(
        url,
        {"jsonrpc": "2.0", "id": name, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
        session_id=session_id,
    )
    assert status == 200
    return body


def test_mcp_surface_advertises_durable_tools_and_rejects_unknown_sessions(tmp_path) -> None:
    servers = start_servers(tmp_path)
    try:
        coordinator, coordinator_thread, mcp, mcp_thread, url = servers
        session_id = initialize(url)
        status, _, listed = post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session_id=session_id)
        assert status == 200
        assert {item["name"] for item in listed["result"]["tools"]} >= {
            "agent_status",
            "invoke_agent",
            "submit",
            "run",
            "Agent",
            "dispatch",
            "inspect",
            "watch",
            "cancel",
            "chain_submit",
        }
        status, _, unknown = post(url, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, session_id="unknown-session")
        assert status == 200
        assert unknown["error"]["code"] == -32600
    finally:
        stop_servers(*servers[:4])


def test_mcp_direct_agent_tools_return_status_and_normalized_receipt(tmp_path) -> None:
    class FakeInvoker:
        config = AgentInvocationConfig(
            workspace_root=tmp_path,
            timeout_seconds=10,
            max_output_chars=4_000,
            max_concurrency=1,
        )

        def status(self):
            return {"agents": [{"agent": "gemini", "readiness": "ready"}]}

        def invoke(self, agent, prompt, **kwargs):
            assert agent == "codex"
            assert prompt == "Return a bounded answer."
            assert kwargs["mode"] == "read-only"
            return AgentInvocationResult(
                agent=agent,
                transport="codex-cli",
                status="PASS",
                summary="codex completed",
                response="CODEX_OK",
                return_code=0,
                duration_seconds=0.1,
                runtime={"read_only": True},
            )

    servers = start_servers(tmp_path, agent_invoker=FakeInvoker())
    try:
        _, _, _, _, url = servers
        session_id = initialize(url)
        status = call(url, session_id, "agent_status", {})
        assert status["result"]["structuredContent"]["agents"][0]["readiness"] == "ready"
        invoked = call(
            url,
            session_id,
            "invoke_agent",
            {"agent": "codex", "prompt": "Return a bounded answer."},
        )
        receipt = invoked["result"]["structuredContent"]
        assert receipt["status"] == "PASS"
        assert receipt["response"] == "CODEX_OK"
    finally:
        stop_servers(*servers[:4])


def test_mcp_direct_agent_write_mode_fails_closed_by_default(tmp_path) -> None:
    servers = start_servers(tmp_path)
    try:
        _, _, _, _, url = servers
        session_id = initialize(url)
        rejected = call(
            url,
            session_id,
            "invoke_agent",
            {"agent": "claude", "prompt": "Edit a file.", "mode": "workspace-write"},
        )
        assert rejected["error"]["code"] == -32602
        assert "workspace-write is disabled" in rejected["error"]["message"]
    finally:
        stop_servers(*servers[:4])


def test_mcp_direct_agent_rejects_non_finite_timeout(tmp_path) -> None:
    servers = start_servers(tmp_path)
    try:
        _, _, _, _, url = servers
        session_id = initialize(url)
        rejected = call(
            url,
            session_id,
            "invoke_agent",
            {"agent": "claude", "prompt": "Reply.", "timeout_seconds": "NaN"},
        )
        assert rejected["error"]["code"] == -32602
        assert "timeout_seconds" in rejected["error"]["message"]
    finally:
        stop_servers(*servers[:4])


def test_mcp_submit_inspect_and_cancel_use_durable_coordinator(tmp_path) -> None:
    servers = start_servers(tmp_path)
    try:
        coordinator, coordinator_thread, mcp, mcp_thread, url = servers
        session_id = initialize(url)
        submitted = call(url, session_id, "submit", {"task": task_payload(), "priority": 4})
        structured = submitted["result"]["structuredContent"]
        assert structured["created"] is True
        assert structured["task_id"] == "mcp-task"
        assert structured["envelope"]["priority"] == 4

        inspected = call(url, session_id, "inspect", {"task_id": "mcp-task"})
        assert inspected["result"]["structuredContent"]["task"]["task_id"] == "mcp-task"
        cancelled = call(url, session_id, "cancel", {"task_id": "mcp-task"})
        assert cancelled["result"]["structuredContent"]["state"] == "cancelled"
    finally:
        stop_servers(*servers[:4])


def test_mcp_dispatch_bounds_duplicate_ids_and_reports_control_errors(tmp_path) -> None:
    servers = start_servers(tmp_path)
    try:
        coordinator, coordinator_thread, mcp, mcp_thread, url = servers
        session_id = initialize(url)
        duplicate = call(
            url,
            session_id,
            "dispatch",
            {"workers": [{"id": "same", "task": task_payload("one")}, {"id": "same", "task": task_payload("two")}]},
        )
        assert duplicate["error"]["code"] == -32602
        dispatched = call(
            url,
            session_id,
            "dispatch",
            {"max_concurrency": 2, "workers": [{"id": "a", "task": task_payload("dispatch-a")}, {"id": "b", "task": task_payload("dispatch-b")}]},
        )
        assert dispatched["result"]["structuredContent"]["count"] == 2
        assert dispatched["result"]["structuredContent"]["failed"] == 0
        bad_task = call(url, session_id, "submit", {"task": {"task_id": "incomplete"}})
        assert bad_task["error"]["code"] == -32602
    finally:
        stop_servers(*servers[:4])


def test_mcp_chain_submit_exposes_deferred_follow_up_contract(tmp_path) -> None:
    servers = start_servers(tmp_path)
    try:
        coordinator, coordinator_thread, mcp, mcp_thread, url = servers
        session_id = initialize(url)
        root = call(
            url,
            session_id,
            "chain_submit",
            {"chain_id": "mcp-chain", "step_id": "build", "step_index": 0, "task": task_payload("chain-build")},
        )
        assert root["result"]["structuredContent"]["created"] is True
        child = call(
            url,
            session_id,
            "chain_submit",
            {
                "chain_id": "mcp-chain",
                "step_id": "review",
                "step_index": 1,
                "task": task_payload("chain-review"),
                "predecessor_task_id": "chain-build",
                "parent_messages": ["Review only the bounded build result."],
                "defer_until_ready": True,
            },
        )
        assert child["result"]["structuredContent"]["pending"] is True
    finally:
        stop_servers(*servers[:4])


def test_mcp_prompt_mode_builds_a_read_only_contract_by_default() -> None:
    task = _task_from_arguments({"prompt": "Inspect the repository and report its test command."})
    assert task["objective"] == "Inspect the repository and report its test command."
    assert task["allowed_files"] == []
    assert "read-only" in task["constraints"][0]


def test_mcp_non_loopback_bind_requires_authentication(tmp_path) -> None:
    with pytest.raises(ValueError, match="auth token"):
        create_mcp_server(
            host="0.0.0.0",
            port=0,
            coordinator_url="http://127.0.0.1:8788",
            auth_token=None,
        )


def test_mcp_local_worker_mode_returns_execution_snapshot(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("agent_relay.mcp.run_worker_forever", lambda config: None)
    coordinator = create_server(
        host="127.0.0.1",
        port=0,
        database=tmp_path / "relay.sqlite3",
        auth_token="coordinator-secret",
    )
    coordinator_thread = Thread(target=coordinator.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    coordinator_thread.start()
    coordinator_url = f"http://127.0.0.1:{coordinator.server_address[1]}"
    local_worker = WorkerConfig(
        coordinator_url=coordinator_url,
        auth_token="coordinator-secret",
        worker_id="mcp-local-worker",
        repo=tmp_path,
        backend="local-qwen",
    )
    mcp = create_mcp_server(
        host="127.0.0.1",
        port=0,
        coordinator_url=coordinator_url,
        coordinator_token="coordinator-secret",
        auth_token="mcp-secret",
        local_worker=local_worker,
    )
    mcp_thread = Thread(target=mcp.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    mcp_thread.start()
    url = f"http://127.0.0.1:{mcp.server_address[1]}/mcp"
    try:
        session_id = initialize(url)
        result = call(
            url,
            session_id,
            "run",
            {"task": task_payload("local-run"), "wait": True, "timeout_seconds": 0},
        )["result"]["structuredContent"]
        assert result["execution_mode"] == "local-worker"
        assert result["execution"]["timed_out"] is True
        assert result["submission"]["task_id"] == "local-run"
    finally:
        stop_servers(coordinator, coordinator_thread, mcp, mcp_thread)


def test_mcp_remote_worker_preserves_remote_workdir_policy(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("agent_relay.mcp.run_worker_forever", lambda config: None)
    local_worker = WorkerConfig(
        coordinator_url="http://127.0.0.1:1",
        auth_token="coordinator-secret",
        worker_id="mcp-remote-worker",
        repo=tmp_path,
        backend="claude-mcp",
    )
    servers = start_servers(tmp_path, local_worker=local_worker)
    try:
        coordinator, coordinator_thread, mcp, mcp_thread, url = servers
        session_id = initialize(url)
        result = call(
            url,
            session_id,
            "run",
            {"prompt": "Read the remote workspace and report.", "workdir": "/remote/project", "timeout_seconds": 0},
        )["result"]["structuredContent"]
        assert result["submission"]["envelope"]["workspace_policy"] == {"mcp_workdir": "/remote/project"}
    finally:
        stop_servers(*servers[:4])


def test_mcp_local_worker_mode_completes_a_run_through_the_durable_plane(monkeypatch, tmp_path) -> None:
    repo = tmp_path / "worker-repo"
    repo.mkdir()
    nested = repo / "nested"
    nested.mkdir()
    (nested / "value.py").write_text("VALUE = 1\n", encoding="utf-8")

    def fake_execute(_config, task, **_kwargs):
        return DelegationResult(
            task_id=task.task_id,
            status=ResultStatus.SUCCESS,
            summary="fixture worker completed MCP task",
            files_changed=("value.py",),
            patch="diff --git a/value.py b/value.py\n",
            verification=(VerificationResult('python -c "assert True"', 0),),
            sandbox_mode="fixture",
            metadata={"main_worktree_unchanged": True},
        )

    monkeypatch.setattr(worker_plane, "execute_task", fake_execute)
    def fake_acceptance(_task, _repo, result, **_kwargs):
        metadata = dict(result.metadata)
        metadata["sol_review"] = {
            "lane": "sol-reviewer",
            "status": "PASS",
            "runtime": {"model": "gpt-5.6-sol", "read_only": True},
        }
        metadata["acceptance_gates"] = ["deterministic-verification", "sol-reviewer"]
        return replace(result, metadata=metadata)

    monkeypatch.setattr(worker_plane, "enforce_acceptance", fake_acceptance)

    def bounded_worker(config):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if worker_plane.run_worker_once(config):
                return
            time.sleep(config.poll_seconds)

    monkeypatch.setattr("agent_relay.mcp.run_worker_forever", bounded_worker)
    coordinator = create_server(
        host="127.0.0.1",
        port=0,
        database=tmp_path / "relay.sqlite3",
        auth_token="coordinator-secret",
    )
    coordinator_thread = Thread(target=coordinator.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    coordinator_thread.start()
    coordinator_url = f"http://127.0.0.1:{coordinator.server_address[1]}"
    local_worker = WorkerConfig(
        coordinator_url=coordinator_url,
        auth_token="coordinator-secret",
        worker_id="mcp-fixture-worker",
        repo=repo,
        backend="claude-task",
        poll_seconds=0.05,
    )
    mcp = create_mcp_server(
        host="127.0.0.1",
        port=0,
        coordinator_url=coordinator_url,
        coordinator_token="coordinator-secret",
        auth_token="mcp-secret",
        local_worker=local_worker,
    )
    mcp_thread = Thread(target=mcp.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    mcp_thread.start()
    url = f"http://127.0.0.1:{mcp.server_address[1]}/mcp"
    try:
        session_id = initialize(url)
        result = call(
            url,
            session_id,
            "run",
            {
                "prompt": "Make the bounded fixture change and verify it.",
                "task_id": "local-run-e2e",
                "allowed_files": ["value.py"],
                "workdir": "nested",
                "wait": True,
                "timeout_seconds": 5,
                "interval_seconds": 0.05,
            },
        )["result"]["structuredContent"]
        assert result["execution_mode"] == "local-worker"
        assert result["execution"]["terminal"] is True
        assert result["execution"]["snapshot"]["state"] == "succeeded"
        assert result["execution"]["snapshot"]["receipt"]["actor"] == "mcp-fixture-worker"
        assert result["execution"]["snapshot"]["task"]["objective"] == "Make the bounded fixture change and verify it."
        assert result["execution"]["snapshot"]["receipt"]["workspace"]["repo"] == str(nested.resolve())
        outside = call(
            url,
            session_id,
            "run",
            {"prompt": "This must not leave the configured worker repository.", "workdir": str(tmp_path)},
        )
        assert outside["error"]["code"] == -32602
    finally:
        stop_servers(coordinator, coordinator_thread, mcp, mcp_thread)
