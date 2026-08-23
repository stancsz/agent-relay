from pathlib import Path
from dataclasses import replace
from argparse import Namespace
import os
import subprocess
import sys
import threading

SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "lanes" / "claude-task" / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from claude_a2a_server import A2AServer, A2AState  # noqa: E402

from agent_relay.claude_task import (
    babystep_progress_contract,
    ClaudeTaskConfig,
    build_claude_task_packet,
    parse_collaboration_handoff,
    parse_progress_evidence,
    run_claude_task,
    _run_async_bridge_job,
    _transport_failure_metadata,
)
from agent_relay.result import ResultStatus
from agent_relay.task import DelegationTask
import agent_relay.claude_task as claude_task_module


def _task() -> DelegationTask:
    return DelegationTask(
        task_id="claude-packet",
        objective="Change the value.",
        allowed_files=("value.py",),
        context=("value.py",),
        requirements=("The value is two.",),
        constraints=("Do not touch unrelated files.",),
        verification=("python -c \"from value import VALUE; assert VALUE == 2\"",),
        task_kind="bounded_bugfix",
    )


def test_build_claude_task_packet_is_bounded_and_digest_linked(tmp_path: Path) -> None:
    (tmp_path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")

    packet = build_claude_task_packet(tmp_path, _task())

    assert packet["protocol"] == "claude-a2a/0.1"
    assert packet["target_role"] == "worker"
    assert packet["workspace"] == {"path": ".", "target_paths": ["value.py"]}
    assert packet["verification"] == ["python -c \"from value import VALUE; assert VALUE == 2\""]
    assert packet["inputs"][0]["path"] == "value.py"
    assert packet["collaboration"]["mode"] == "bounded-remote"
    assert "questions_for_orchestrator" in packet["collaboration"]["handoff_fields"]
    assert packet["progress_contract"]["mode"] == "babystep-evidence"
    assert packet["progress_contract"]["steps"] == ["inspect", "plan", "execute", "verify", "handoff"]
    assert len(packet["context_digest"]) == 64
    assert "conversation" not in packet


def test_parse_collaboration_handoff_extracts_labeled_parent_questions() -> None:
    report = """observed_remote_state: target is present
assumptions: only the declared file is authoritative
questions_for_orchestrator: should the generated fixture be retained?
recommended_next_prompt: confirm the retention policy
verification_evidence: pytest -q (exit 0)
blockers: none
"""

    handoff = parse_collaboration_handoff(report)

    assert handoff["questions_for_orchestrator"].startswith("should the generated")
    assert handoff["recommended_next_prompt"] == "confirm the retention policy"
    assert handoff["verification_evidence"] == "pytest -q (exit 0)"


def test_parse_progress_evidence_requires_concrete_lines_for_every_babystep() -> None:
    report = """PROGRESS inspect | status=done | evidence=read value.py
PROGRESS plan | status=done | evidence=decided VALUE must be two
PROGRESS execute | status=done | evidence=changed value.py only
PROGRESS verify | status=done | evidence=pytest -q exit_code=0
PROGRESS handoff | status=done | evidence=reported patch and verification
"""

    parsed = parse_progress_evidence(report)

    assert set(parsed) == set(babystep_progress_contract()["steps"])
    assert parsed["verify"]["evidence"] == "pytest -q exit_code=0"


def test_remote_result_fails_closed_when_babystep_evidence_is_missing(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        claude_task_module,
        "_run_async_bridge_job",
        lambda *_args, **_kwargs: {
            "status": "done",
            "output": "worker says done without an auditable progress receipt",
            "changed_paths": [],
            "patch": "",
            "server_receipt": {"transport": "cli-fallback", "accepted_by_transport": True},
        },
    )

    result = run_claude_task(
        replace(_task(), verification=()),
        tmp_path,
        config=ClaudeTaskConfig(
            remote_url="https://pc-b.example.test:8787",
            remote_auth_token="remote-secret",
        ),
    )

    assert result.status is ResultStatus.FAILED_VERIFICATION
    assert result.metadata["progress_evidence"]["satisfied"] is False
    assert set(result.metadata["progress_evidence"]["missing_steps"]) == {
        "inspect", "plan", "execute", "verify", "handoff"
    }
    assert "babystep progress evidence is incomplete" in result.blockers


def test_claude_task_missing_bridge_is_explicit_and_preserves_main_repo(tmp_path: Path) -> None:
    (tmp_path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    config = ClaudeTaskConfig(bridge_script=tmp_path / "missing-server.py")

    result = run_claude_task(_task(), tmp_path, config=config)

    assert result.status is ResultStatus.WORKER_ERROR
    assert result.metadata["main_worktree_unchanged"] is True
    assert "bridge is unavailable" in result.summary
    assert result.metadata["retryable"] is False


def test_claude_transport_timeout_is_marked_retryable() -> None:
    metadata = _transport_failure_metadata(
        claude_task_module.ClaudeTaskError(
            "Claude bridge request GET http://bridge/a2a/jobs/job-1 failed: timed out"
        )
    )

    assert metadata["failure_kind"] == "bridge_transport"
    assert metadata["retryable"] is True


def test_claude_task_invalid_timeout_environment_uses_safe_default(monkeypatch) -> None:
    monkeypatch.setenv("AR_CLAUDE_TIMEOUT_SECONDS", "not-a-number")

    config = ClaudeTaskConfig.from_env()

    assert config.timeout_seconds == 300.0


def test_claude_task_config_reads_remote_bridge_settings(monkeypatch) -> None:
    monkeypatch.setenv("AR_CLAUDE_A2A_SERVER_URL", "https://pc-b.example.test:8787")
    monkeypatch.setenv("AR_CLAUDE_A2A_AUTH_TOKEN", "remote-secret")
    monkeypatch.setenv("AR_CLAUDE_A2A_WORKSPACE_PATH", "repos/project")

    config = ClaudeTaskConfig.from_env()

    assert config.remote_url == "https://pc-b.example.test:8787"
    assert config.remote_auth_token == "remote-secret"
    assert config.remote_workspace_path == "repos/project"


def test_claude_task_can_dispatch_to_existing_remote_bridge(tmp_path: Path, monkeypatch) -> None:
    captured = {}
    (tmp_path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")

    def fake_async(base, packet, *, timeout_seconds, cancel_event, auth_token=None):
        captured.update({"base": base, "packet": packet, "auth_token": auth_token})
        return {
            "status": "done",
            "output": "PROGRESS inspect | status=done | evidence=read value.py\nPROGRESS plan | status=done | evidence=planned bounded change\nPROGRESS execute | status=not_applicable | evidence=remote fixture returned no patch\nPROGRESS verify | status=done | evidence=parent verification has no commands\nPROGRESS handoff | status=done | evidence=returned bounded receipt",
            "changed_paths": [],
            "patch": "",
            "server_receipt": {"transport": "mcp", "accepted_by_transport": True},
        }

    monkeypatch.setattr(claude_task_module, "_run_async_bridge_job", fake_async)
    task = replace(_task(), verification=())
    result = run_claude_task(
        task,
        tmp_path,
        config=ClaudeTaskConfig(
            remote_url="https://pc-b.example.test:8787",
            remote_auth_token="remote-secret",
            remote_workspace_path="repos/project",
        ),
    )

    assert result.status is ResultStatus.SUCCESS
    assert result.patch == ""
    assert result.metadata["transport"] == "remote-claude-a2a"
    assert result.metadata["verification_authority"] == "parent-local-sandbox"
    assert result.verification == ()
    assert captured["base"] == "https://pc-b.example.test:8787"
    assert captured["auth_token"] == "remote-secret"
    assert captured["packet"]["workspace"]["path"] == "repos/project"


def test_remote_claude_bridge_rejects_non_loopback_plain_http(tmp_path: Path) -> None:
    result = run_claude_task(
        _task(),
        tmp_path,
        config=ClaudeTaskConfig(remote_url="http://pc-b.example.test:8787"),
    )

    assert result.status is ResultStatus.WORKER_ERROR
    assert "limited to loopback" in result.summary


def test_remote_claude_result_runs_parent_owned_verification_and_scope_gate(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        claude_task_module,
        "_run_async_bridge_job",
        lambda *_args, **_kwargs: {
            "status": "done",
            "output": "PROGRESS inspect | status=done | evidence=read value.py\nPROGRESS plan | status=done | evidence=planned VALUE update\nPROGRESS execute | status=done | evidence=returned value.py patch\nPROGRESS verify | status=done | evidence=parent ran declared check\nPROGRESS handoff | status=done | evidence=returned patch and receipt",
            "changed_paths": ["value.py"],
            "patch": (
                "diff --git a/value.py b/value.py\n"
                "--- a/value.py\n"
                "+++ b/value.py\n"
                "@@ -1 +1 @@\n"
                "-VALUE = 1\n"
                "+VALUE = 2\n"
            ),
            "server_receipt": {"transport": "cli-fallback", "accepted_by_transport": True},
        },
    )

    result = run_claude_task(
        replace(_task(), task_id="remote-parent-verification"),
        tmp_path,
        config=ClaudeTaskConfig(
            remote_url="https://pc-b.example.test:8787",
            remote_auth_token="remote-secret",
            remote_workspace_path=".",
        ),
    )

    assert result.status is ResultStatus.SUCCESS
    assert result.files_changed == ("value.py",)
    assert result.verification and result.verification[0].passed
    assert result.metadata["verification_authority"] == "parent-local-sandbox"
    assert result.metadata["main_worktree_unchanged"] is True
    assert (tmp_path / "value.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_claude_task_executes_through_a_real_remote_a2a_daemon(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "remote-repo"
    repo.mkdir()
    (repo / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    for args in (
        ("init", "--quiet"),
        ("config", "user.email", "agent-relay@example.invalid"),
        ("config", "user.name", "agent-relay-test"),
        ("add", "value.py"),
        ("commit", "--quiet", "-m", "init"),
    ):
        completed = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=False
        )
        assert completed.returncode == 0, completed.stderr

    fixture = SCRIPT_ROOT / "test-fixtures" / "fake-mcp-success"
    monkeypatch.setenv("PATH", str(fixture) + os.pathsep + os.environ.get("PATH", ""))
    state = A2AState(
        Namespace(
            workspace_root=str(repo),
            auth_token="remote-secret",
            worker_agent_type=None,
            verifier_agent_type=None,
            agents_json=None,
            cli_fallback=False,
            no_cli_fallback=False,
            timeout_seconds=30,
            state_dir=str(tmp_path / "remote-state"),
        )
    )
    server = A2AServer(("127.0.0.1", 0), state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = run_claude_task(
            replace(_task(), task_id="real-remote-claude", verification=()),
            repo,
            config=ClaudeTaskConfig(
                remote_url=f"http://127.0.0.1:{server.server_port}",
                remote_auth_token="remote-secret",
                timeout_seconds=30,
            ),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.status is ResultStatus.SUCCESS
    assert result.metadata["transport"] == "remote-claude-a2a"
    assert result.metadata["server_receipt"]["accepted_by_transport"] is True
    assert result.metadata["main_worktree_unchanged"] is True


def test_async_claude_job_sends_cancel_and_returns_stopped_status(monkeypatch) -> None:
    calls = []
    responses = iter([
        {"job_id": "job-1", "status": "queued"},
        {"job_id": "job-1", "status": "cancelled", "result": {"status": "failed", "output": "stopped"}},
    ])

    def fake_request(url, method, body=None, timeout=10.0):
        calls.append((url, method))
        if url.endswith("/cancel"):
            return {"job_id": "job-1", "status": "cancel_requested"}
        if method == "POST":
            return next(responses)
        return next(responses)

    monkeypatch.setattr(claude_task_module, "_request_json", fake_request)
    cancel_event = threading.Event()
    cancel_event.set()
    result = _run_async_bridge_job(
        "http://bridge",
        {"task_id": "task-1", "context_digest": "digest"},
        timeout_seconds=1,
        cancel_event=cancel_event,
    )

    assert result["status"] == "cancelled"
    assert any(url.endswith("/a2a/jobs/job-1/cancel") for url, _method in calls)


def test_claude_task_wraps_bridge_result_in_sandbox_and_parent_verification(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    task = replace(_task(), verification=("python -c \"assert True\"",))

    class FakeProcess:
        def poll(self):
            return None

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(
        claude_task_module,
        "_start_bridge",
        lambda *_args, **_kwargs: (FakeProcess(), "http://127.0.0.1:1"),
    )
    monkeypatch.setattr(
        claude_task_module,
        "_request_json",
        lambda *_args, **_kwargs: {
            "status": "done",
            "output": "PROGRESS inspect | status=done | evidence=read value.py\nPROGRESS plan | status=done | evidence=planned bounded task\nPROGRESS execute | status=not_applicable | evidence=fake bridge made no edit\nPROGRESS verify | status=done | evidence=parent ran pytest exit_code=0\nPROGRESS handoff | status=done | evidence=returned bounded task receipt",
            "server_receipt": {"transport": "cli-fallback"},
        },
    )

    result = claude_task_module.run_claude_task(task, tmp_path)

    assert result.status is ResultStatus.SUCCESS
    assert result.metadata["main_worktree_unchanged"] is True
    assert result.metadata["lane"] == "claude-task"
    assert result.files_changed == ()
