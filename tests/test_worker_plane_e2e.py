from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from threading import Thread

from agent_relay.control import create_server, request_json, stream_events
from agent_relay.protocol import JobState
from agent_relay.result import DelegationResult, ResultStatus, VerificationResult
import agent_relay.worker_plane as worker_plane


def _task_payload() -> dict[str, object]:
    return {
        "task_id": "remote-claude-worker-e2e",
        "objective": "Perform one bounded remote worker operation.",
        "allowed_files": ["value.py"],
        "verification": ["python -c \"assert True\""],
        "task_kind": "mechanical",
    }


def test_real_http_coordinator_and_worker_plane_complete_with_receipt_and_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "worker-repo"
    repo.mkdir()
    (repo / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    server = create_server(
        host="127.0.0.1",
        port=0,
        database=tmp_path / "relay.sqlite3",
        auth_token="admin-secret",
    )
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def fake_execute(_config, task, **_kwargs):
        return DelegationResult(
            task_id=task.task_id,
            status=ResultStatus.SUCCESS,
            summary="remote worker completed bounded operation",
            files_changed=("value.py",),
            patch="diff --git a/value.py b/value.py\n",
            verification=(VerificationResult("python -c \\\"assert True\\\"", 0),),
            sandbox_mode="fixture",
            metadata={
                "main_worktree_unchanged": True,
                "lane": "claude-mcp",
                "transport": "streamable-http-mcp",
                "remote_endpoint": "https://claude.example.test/mcp",
                "verification_authority": "remote-mcp-output-only",
            },
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
    try:
        request_json(
            base,
            "POST",
            "/tasks",
            auth_token="admin-secret",
            payload={"task": _task_payload(), "idempotency_key": "remote-claude-worker-e2e"},
        )
        outcomes = worker_plane.run_worker_once(
            worker_plane.WorkerConfig(
                coordinator_url=base,
                auth_token="admin-secret",
                agent_token="worker-secret",
                worker_id="pc-b-claude",
                repo=repo,
                backend="claude-task",
                poll_seconds=0.05,
            )
        )

        assert outcomes[0]["status"] == JobState.SUCCEEDED.value
        envelope = request_json(
            base,
            "GET",
            "/tasks/remote-claude-worker-e2e",
            auth_token="admin-secret",
        )
        assert envelope["state"] == JobState.SUCCEEDED.value
        assert envelope["receipt"]["final_state"] == JobState.SUCCEEDED.value
        assert envelope["receipt"]["evidence"]["transport"] == "streamable-http-mcp"
        assert envelope["receipt"]["evidence"]["remote_endpoint"] == "https://claude.example.test/mcp"
        assert envelope["receipt"]["evidence"]["verification_authority"] == "remote-mcp-output-only"
        artifacts = request_json(
            base,
            "GET",
            "/tasks/remote-claude-worker-e2e/artifacts",
            auth_token="admin-secret",
        )
        assert len(artifacts["artifacts"]) == 1
        assert artifacts["artifacts"][0]["provenance"] == "pc-b-claude"
        events = list(
            stream_events(
                base,
                "/tasks/remote-claude-worker-e2e/events/stream?after=0&timeout=1",
                auth_token="admin-secret",
            )
        )
        assert events[-1]["data"]["state"] == JobState.SUCCEEDED.value
        assert any(
            item["data"].get("actor") == "pc-b-claude" for item in events
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
