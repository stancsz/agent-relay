from __future__ import annotations

import hashlib
import json
import os
import subprocess
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from argparse import Namespace
from pathlib import Path

from a2a_protocol import ProtocolError, build_task, digest_without_context_digest, validate_task
import claude_a2a_server as a2a_server
from claude_a2a_server import A2AServer, A2AState, is_client_disconnect, is_native_capability_failure
import claude_mcp_delegate as mcp_delegate


def post(url: str, payload: dict, token: str | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def git(*args: str, cwd: Path) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr)


def test_agent_type_default_omission() -> None:
    """Regression: default team manifest + Agent call must not invent general-purpose.

    Older Claude Code releases (including 2.1.220) reject MCP Agent
    subagent_type=general-purpose. The
    server previously forced every team member to that value when no
    --worker-agent-type / --verifier-agent-type was configured. The fix is to
    leave agent_type unset on the manifest (and therefore omit subagent_type
    on the Agent call) for the default case, while preserving the explicit
    custom-agent-type path.
    """
    workspace = Path(tempfile.mkdtemp(prefix="agent-type-regress-"))
    (workspace / "target.txt").write_text("stable\n", encoding="utf-8")
    git("init", "--quiet", cwd=workspace)
    git("config", "user.email", "a2a@example.invalid", cwd=workspace)
    git("config", "user.name", "a2a-test", cwd=workspace)
    git("add", "target.txt", cwd=workspace)
    git("commit", "--quiet", "-m", "init", cwd=workspace)

    team_task = build_task(
        task_id="agent-type-default-team",
        target_role="team",
        operation="team",
        target_paths=["target.txt"],
        objective="No-edit default team smoke for the agent type regression.",
        acceptance_criteria=["No agent_type is forced on members."],
        constraints=["Do not modify files."],
        inputs=[],
        team={"name": "agent-type-default", "members": [
            {"name": "builder", "role": "worker", "objective": "Worker no-op."},
            {"name": "reviewer", "role": "verifier", "objective": "Verifier no-op."},
        ]},
        expected_change=False,
    )

    captured = []

    class _Proc:
        pid = 12345

        def __init__(self, command):
            self.command = command
            captured.append({"command": command, "stdin": None})

        def communicate(self, input=None, timeout=None):
            captured[-1]["stdin"] = input
            return "", ""

        def poll(self):
            return 0

        @property
        def returncode(self):
            return 0

    def fake_process(command, **_kwargs):
        return _Proc(command)

    real_popen = subprocess.Popen
    real_git_snapshot = a2a_server.git_snapshot
    a2a_server.git_snapshot = lambda _workspace, _extra_paths=None: {
        "head": "test-head",
        "status_paths": [],
        "change_fingerprint": "test-fingerprint",
    }
    subprocess.Popen = fake_process
    try:
        case_default = A2AState(Namespace(
            workspace_root=str(workspace), auth_token="lan-secret",
            worker_agent_type=None, verifier_agent_type=None,
            timeout_seconds=None, state_dir=str(Path(tempfile.mkdtemp(prefix="state-default-"))),
        ))
        try:
            case_default.run_task(team_task)
        except Exception:
            pass
        default_calls = [c for c in captured if "--team-mode" in c["command"]]
        assert default_calls, "team-mode subprocess was not captured"
        default_manifest = json.loads(default_calls[-1]["stdin"])
        for member in default_manifest["members"]:
            assert "agent_type" not in member, (
                f"default team manifest must not invent agent_type; got {member}"
            )

        case_explicit = A2AState(Namespace(
            workspace_root=str(workspace), auth_token="lan-secret",
            worker_agent_type="custom-worker",
            verifier_agent_type="custom-verifier",
            timeout_seconds=None, state_dir=str(Path(tempfile.mkdtemp(prefix="state-explicit-"))),
        ))
        try:
            case_explicit.run_task(team_task)
        except Exception:
            pass
        explicit_calls = [c for c in captured[1:] if "--team-mode" in c["command"]]
        assert explicit_calls, "explicit-config team-mode subprocess was not captured"
        explicit_manifest = json.loads(explicit_calls[-1]["stdin"])
        types_by_role = {member["role"]: member.get("agent_type") for member in explicit_manifest["members"]}
        assert types_by_role.get("worker") == "custom-worker", types_by_role
        assert types_by_role.get("verifier") == "custom-verifier", types_by_role

        single_task = build_task(
            task_id="agent-type-default-worker",
            target_role="worker",
            operation="work",
            target_paths=["target.txt"],
            objective="No-edit default worker smoke for the agent type regression.",
            acceptance_criteria=["--agent-type is not passed by default."],
            constraints=["Do not modify files."],
            inputs=[],
            expected_change=False,
        )

        captured.clear()
        try:
            case_default.run_task(single_task)
        except Exception:
            pass
        single_calls = [c for c in captured if "--team-mode" not in c["command"]]
        assert single_calls, "single-mode subprocess was not captured"
        default_command = single_calls[-1]["command"]
        assert "--agent-type" not in default_command, (
            f"default single-mode command must not pass --agent-type; got {default_command}"
        )

        captured.clear()
        try:
            case_explicit.run_task(single_task)
        except Exception:
            pass
        explicit_single_calls = [c for c in captured if "--team-mode" not in c["command"]]
        assert explicit_single_calls, "explicit single-mode subprocess was not captured"
        explicit_command = explicit_single_calls[-1]["command"]
        assert "--agent-type" in explicit_command, (
            f"explicit single-mode command must pass --agent-type; got {explicit_command}"
        )
        assert "custom-worker" in explicit_command, explicit_command
    finally:
        subprocess.Popen = real_popen
        a2a_server.git_snapshot = real_git_snapshot

    # Downstream MCP delegate: run_team() must conditionally set subagent_type.
    manifest_default = {
        "team_name": "regression-default",
        "description": "default",
        "shared": {"task_id": "t", "context_digest": "0" * 64, "objective": "o", "target_paths": [],
                   "acceptance_criteria": [], "constraints": [], "inputs": [], "profile_context": {}},
        "members": [
            {"name": "builder", "role": "worker", "objective": "w"},
            {"name": "reviewer", "role": "verifier", "objective": "v"},
        ],
    }
    manifest_explicit = json.loads(json.dumps(manifest_default))
    for member in manifest_explicit["members"]:
        member["agent_type"] = "custom-worker" if member["role"] == "worker" else "custom-verifier"

    class _FakeSession:
        def __init__(self):
            self.calls = []
            self.tools = [{"name": name} for name in ("Agent", "TaskCreate", "TaskUpdate", "TaskList", "SendMessage")]
            self.team_name = None
            self.team_file_path = None
            self.spawned_names = []
            self.team_complete = False
            self.native_team_mode = "implicit-agent"
            self.initialize = {"result": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fake", "version": "test"}}}
            self.started = time.monotonic()
            self.timeout_seconds = None
            self.process = type("P", (), {"returncode": 0})()

        def deadline(self):
            return None

        def call(self, name, arguments, deadline=None):
            self.calls.append((name, arguments))
            if name == "Agent":
                return {"result": {"content": [{"type": "text", "text": json.dumps({"status": "ok", "team_name": "regression-default"})}]}}
            if name == "TaskCreate":
                return {"result": {"content": [{"type": "text", "text": json.dumps({"task": {"id": "x"}})}]}}
            if name in {"TaskUpdate", "TaskList", "SendMessage"}:
                return {"result": {"content": [{"type": "text", "text": json.dumps({"ok": True})}]}}
            return {"result": {"content": [{"type": "text", "text": ""}]}}

    team_dirs_root = Path(tempfile.mkdtemp(prefix="regression-team-"))
    old_team_state = os.environ.get("CLAUDE_TEAM_STATE_ROOT")
    os.environ["CLAUDE_TEAM_STATE_ROOT"] = str(team_dirs_root)

    def prepare_fake_session(session):
        team_dir = team_dirs_root / "regression-default"
        inbox_dir = team_dir / "inboxes"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        (team_dir / "config.json").write_text("{}", encoding="utf-8")
        (inbox_dir / "team-lead.json").write_text(json.dumps([
            {"from": "builder", "text": "A2A_RESULT FAKE_BUILDER"},
            {"from": "reviewer", "text": "A2A_RESULT FAKE_REVIEWER"},
        ]), encoding="utf-8")
        session.team_file_path = team_dir / "config.json"

    try:
        session_default = _FakeSession()
        prepare_fake_session(session_default)
        result_text, protocol_error, _ = mcp_delegate.run_team(session_default, manifest_default)
        assert protocol_error is None, protocol_error
        agent_calls_default = [arguments for name, arguments in session_default.calls if name == "Agent"]
        assert agent_calls_default, "Agent was not called for the default manifest"
        for arguments in agent_calls_default:
            assert "subagent_type" not in arguments, (
                f"default Agent call must not include subagent_type; got {arguments}"
            )

        session_explicit = _FakeSession()
        prepare_fake_session(session_explicit)
        result_text, protocol_error, _ = mcp_delegate.run_team(session_explicit, manifest_explicit)
        assert protocol_error is None, protocol_error
        agent_calls_explicit = [arguments for name, arguments in session_explicit.calls if name == "Agent"]
        assert agent_calls_explicit, "Agent was not called for the explicit manifest"
        for arguments in agent_calls_explicit:
            assert arguments.get("subagent_type"), (
                f"explicit Agent call must include subagent_type; got {arguments}"
            )
    finally:
        if old_team_state is None:
            os.environ.pop("CLAUDE_TEAM_STATE_ROOT", None)
        else:
            os.environ["CLAUDE_TEAM_STATE_ROOT"] = old_team_state
        shutil.rmtree(team_dirs_root, ignore_errors=True)


def test_native_capability_fallback() -> None:
    """A missing native team surface may use the explicit bounded CLI route."""
    workspace = Path(tempfile.mkdtemp(prefix="capability-fallback-"))
    try:
        state = A2AState(Namespace(
            workspace_root=str(workspace), auth_token="lan-secret",
            worker_agent_type=None, verifier_agent_type=None,
            timeout_seconds=None, state_dir=None,
        ))
        task = build_task(
            task_id="capability-fallback",
            target_role="team",
            operation="team",
            target_paths=["README.md"],
            objective="Exercise the bounded native-capability fallback.",
            acceptance_criteria=["The CLI fallback is clearly labeled."],
            constraints=["Do not edit files."],
            inputs=[],
            team={"name": "capability-fallback", "members": [
                {"name": "builder", "role": "worker", "objective": "No-op."},
                {"name": "reviewer", "role": "verifier", "objective": "No-op."},
            ]},
            expected_change=False,
        )

        native = {
            "protocol": task["protocol"],
            "task_id": task["task_id"],
            "target_role": task["target_role"],
            "status": "failed",
            "output": "native Agent Teams are unavailable; missing MCP tools: TaskCreate",
            "changed_paths": [],
            "evidence": [],
            "context_digest": task["context_digest"],
            "server_receipt": {
                "transport": "mcp",
                "protocol_error": "native Agent Teams are unavailable; missing MCP tools: TaskCreate",
                "worktree_changed": False,
            },
        }
        fallback = {
            "protocol": task["protocol"],
            "task_id": task["task_id"],
            "target_role": task["target_role"],
            "status": "done",
            "output": "bounded CLI result",
            "changed_paths": [],
            "evidence": [],
            "context_digest": task["context_digest"],
            "server_receipt": {
                "transport": "cli-fallback",
                "worktree_changed": False,
            },
        }
        state._run_mcp_task_locked = lambda *_args, **_kwargs: native
        state._run_cli_fallback_locked = lambda *_args, **_kwargs: fallback
        result = state._run_task_locked(task, workspace)

        assert is_native_capability_failure(native["server_receipt"]["protocol_error"])
        assert result["status"] == "done", result
        assert result["server_receipt"]["transport"] == "cli-fallback"
        assert result["server_receipt"]["native_attempt"]["transport"] == "mcp"
        assert result["evidence"][0]["kind"] == "transport-fallback"
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        shutil.rmtree(workspace, ignore_errors=True)


def test_cli_verifier_cannot_mutate_caller_workspace() -> None:
    """A verifier's shell-side write must stay inside its disposable copy."""
    workspace = Path(tempfile.mkdtemp(prefix="cli-verifier-isolation-"))
    try:
        target = workspace / "target.txt"
        target.write_text("stable\n", encoding="utf-8")
        git("init", "--quiet", cwd=workspace)
        git("config", "user.email", "a2a@example.invalid", cwd=workspace)
        git("config", "user.name", "a2a-test", cwd=workspace)
        git("add", "target.txt", cwd=workspace)
        git("commit", "--quiet", "-m", "init", cwd=workspace)

        state = A2AState(Namespace(
            workspace_root=str(workspace), auth_token="lan-secret",
            worker_agent_type=None, verifier_agent_type=None,
            timeout_seconds=None, state_dir=None,
        ))
        task = build_task(
            task_id="cli-verifier-isolation",
            target_role="verifier",
            operation="verify",
            target_paths=["target.txt"],
            objective="Inspect the target without changing the caller workspace.",
            acceptance_criteria=["The caller target remains stable."],
            constraints=["Read-only verifier."],
            inputs=[],
            expected_change=False,
        )
        seen_workspaces: list[Path] = []

        def fake_delegate(verifier_workspace: Path, *_args, **_kwargs):
            seen_workspaces.append(verifier_workspace)
            (verifier_workspace / "target.txt").write_text("mutated by verifier\n", encoding="utf-8")
            return ({"accepted": True, "unexpected_worktree_change": True, "branch_or_head_changed": False}, 0)

        state._run_cli_delegate_once = fake_delegate  # type: ignore[method-assign]
        result = state._run_cli_fallback_locked(task, workspace)

        assert result["status"] == "failed", result
        assert result["server_receipt"]["verifier_clean"] is False, result
        assert target.read_text(encoding="utf-8") == "stable\n"
        assert seen_workspaces and seen_workspaces[0] != workspace
        assert not seen_workspaces[0].exists()
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_cli_verifier_empty_source_set_does_not_clone_dirty_workspace() -> None:
    """An explicit empty verifier source set must stay an empty temp repo."""
    workspace = Path(tempfile.mkdtemp(prefix="cli-verifier-empty-sources-"))
    try:
        (workspace / "unrelated-large-tree-marker.txt").write_text("caller-only\n", encoding="utf-8")
        with isolated_cli_verifier_workspace(workspace, include_paths=[]) as verifier_workspace:
            assert (verifier_workspace / ".git").is_dir()
            assert not (verifier_workspace / "unrelated-large-tree-marker.txt").exists()
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_team_fallback_verifier_copies_objective_sources() -> None:
    """Team fallback verifiers receive the explicitly named source files."""
    workspace = Path(tempfile.mkdtemp(prefix="cli-team-verifier-sources-"))
    try:
        target = workspace / "target.txt"
        source = workspace / "docs" / "input.md"
        target.write_text("stable\n", encoding="utf-8")
        source.parent.mkdir(parents=True)
        source.write_text("source evidence\n", encoding="utf-8")
        git("init", "--quiet", cwd=workspace)
        git("config", "user.email", "a2a@example.invalid", cwd=workspace)
        git("config", "user.name", "a2a-test", cwd=workspace)
        git("add", "-A", cwd=workspace)
        git("commit", "--quiet", "-m", "init", cwd=workspace)

        state = A2AState(Namespace(
            workspace_root=str(workspace), auth_token="lan-secret",
            worker_agent_type=None, verifier_agent_type=None,
            timeout_seconds=None, state_dir=None,
        ))
        task = build_task(
            task_id="cli-team-verifier-sources",
            target_role="team",
            operation="team",
            target_paths=["target.txt"],
            objective=(
                "Read target.txt and docs/input.md only; verify the declared source "
                "docs/input.md is present in the bounded verifier workspace."
            ),
            acceptance_criteria=["The verifier can read the named source."],
            constraints=["Read-only verifier."],
            inputs=[],
            team={"name": "source-copy", "members": [
                {"name": "writer", "role": "worker", "objective": "No-op."},
                {"name": "reviewer", "role": "verifier", "objective": "Check docs/input.md."},
            ]},
            expected_change=False,
        )
        verifier_workspaces: list[Path] = []

        def fake_delegate(delegate_workspace: Path, *_args, **_kwargs):
            if delegate_workspace != workspace:
                verifier_workspaces.append(delegate_workspace)
                assert (delegate_workspace / "docs" / "input.md").is_file()
            return ({
                "accepted": True,
                "unexpected_worktree_change": False,
                "branch_or_head_changed": False,
                "stdout": "bounded result",
            }, 0)

        state._run_cli_delegate_once = fake_delegate  # type: ignore[method-assign]
        result = state._run_cli_fallback_locked(task, workspace)

        assert result["status"] == "done", result
        assert result["server_receipt"]["verifier_isolated"] is True, result
        assert verifier_workspaces and not verifier_workspaces[0].exists()
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_cli_delegate_timeout_does_not_wait_for_orphaned_pipes() -> None:
    """A dead child with inherited pipes must produce a bounded timeout receipt."""
    workspace = Path(tempfile.mkdtemp(prefix="cli-timeout-pipes-"))

    class _Stream:
        def close(self) -> None:
            return None

    class _HungProcess:
        pid = 54321

        def __init__(self) -> None:
            self.stdout = _Stream()
            self.stderr = _Stream()
            self.killed = False

        def poll(self):
            return 1 if self.killed else None

        @property
        def returncode(self):
            return 1 if self.killed else None

        def kill(self) -> None:
            self.killed = True

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(["fake-claude"], timeout)

    real_popen = subprocess.Popen
    real_run = subprocess.run
    fake_process = _HungProcess()
    subprocess.Popen = lambda *_args, **_kwargs: fake_process
    subprocess.run = lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0)
    started = time.monotonic()
    try:
        state = A2AState(Namespace(
            workspace_root=str(workspace), auth_token=None,
            worker_agent_type=None, verifier_agent_type=None,
            timeout_seconds=-100, state_dir=str(workspace / "state"),
        ))
        receipt, returncode = state._run_cli_delegate_once(
            workspace, "Read the target and return a bounded result.", [],
            allowed_tools="Read", expected_change=False,
        )
    finally:
        subprocess.Popen = real_popen
        subprocess.run = real_run
        shutil.rmtree(workspace, ignore_errors=True)
    assert time.monotonic() - started < 5, "timeout cleanup exceeded the bounded test window"
    assert returncode == 1
    assert receipt.get("timed_out") is True, receipt
    assert receipt.get("accepted") is False, receipt


def test_client_disconnect_classification() -> None:
    assert is_client_disconnect(ConnectionAbortedError(10053, "connection aborted"))
    assert is_client_disconnect(BrokenPipeError())
    assert is_client_disconnect(ConnectionResetError())
    assert not is_client_disconnect(ValueError("not a socket disconnect"))


def main() -> int:
    fixture = Path(__file__).parent / "test-fixtures" / "fake-mcp-success"
    legacy_team_fixture = Path(__file__).parent / "test-fixtures" / "fake-mcp-team"
    modern_team_fixture = Path(__file__).parent / "test-fixtures" / "fake-mcp-modern-team"
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = str(fixture) + os.pathsep + old_path
    with tempfile.TemporaryDirectory(prefix="claude-a2a-test-") as temp:
        root = Path(temp)
        modern_team_root = Path(tempfile.mkdtemp(prefix="claude-a2a-modern-team-"))
        legacy_team_root = Path(tempfile.mkdtemp(prefix="claude-a2a-legacy-team-"))
        durable_root = Path(tempfile.mkdtemp(prefix="claude-a2a-state-"))
        os.environ["CLAUDE_TEAM_STATE_ROOT"] = str(modern_team_root.parent)
        (root / "target.txt").write_text("stable\n", encoding="utf-8")
        git("init", "--quiet", cwd=root)
        git("config", "user.email", "a2a@example.invalid", cwd=root)
        git("config", "user.name", "a2a-test", cwd=root)
        git("add", "target.txt", cwd=root)
        git("commit", "--quiet", "-m", "init", cwd=root)
        state = A2AState(Namespace(workspace_root=str(root), auth_token="lan-secret", worker_agent_type=None, verifier_agent_type=None, timeout_seconds=None, state_dir=str(durable_root)))
        server = A2AServer(("127.0.0.1", 0), state)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            health = json.loads(urllib.request.urlopen(f"{base}/health").read())
            assert health["fresh_mcp_session_per_task"] is True
            assert health["conversation_context_forwarded"] is False

            digest = hashlib.sha256((root / "target.txt").read_bytes()).hexdigest()
            task = build_task(
                task_id="a2a-worker-1",
                target_role="worker",
                operation="work",
                target_paths=["target.txt"],
                objective="Return the MCP smoke response without editing.",
                acceptance_criteria=["The response is exactly the fake result."],
                constraints=["Do not modify files."],
                inputs=[{"path": "target.txt", "sha256": digest, "excerpt": "stable"}],
                expected_change=False,
            )
            status, result = post(f"{base}/a2a/tasks", task, "lan-secret")
            assert status == 200, result
            assert result["status"] == "done", result
            assert result["target_role"] == "worker"
            assert result["context_digest"] == task["context_digest"]
            assert result["server_receipt"]["accepted_by_transport"] is True
            memory_body = json.dumps({"profile": "coder", "text": "Use focused tests before broad validation.", "tags": ["testing"]}).encode("utf-8")
            memory_request = urllib.request.Request(f"{base}/a2a/memory", data=memory_body, headers={"Content-Type": "application/json", "Authorization": "Bearer lan-secret"}, method="POST")
            with urllib.request.urlopen(memory_request) as memory_response:
                assert memory_response.status == 201
            skill_body = json.dumps({"skill_ref": "focused-review", "content": "Run the focused test and inspect the complete diff."}).encode("utf-8")
            skill_request = urllib.request.Request(f"{base}/a2a/profiles/coder/skills", data=skill_body, headers={"Content-Type": "application/json", "Authorization": "Bearer lan-secret"}, method="POST")
            with urllib.request.urlopen(skill_request) as skill_response:
                assert skill_response.status == 201
            memory_task = build_task(
                task_id="a2a-memory-1", target_role="worker", operation="work", target_paths=["target.txt"],
                objective="Return a profile-context smoke response without editing.", acceptance_criteria=["The fake response is returned."],
                constraints=["Do not modify files."], inputs=[], profile="coder", skill_refs=["focused-review"], memory_query="focused tests", remember=True, expected_change=False,
            )
            memory_status, memory_result = post(f"{base}/a2a/tasks", memory_task, "lan-secret")
            assert memory_status == 200 and memory_result["status"] == "done", memory_result
            task_file = root / "task.json"
            task_file.write_text(json.dumps(task), encoding="utf-8")
            client = Path(__file__).with_name("claude_a2a_client.py")
            client_run = subprocess.run([sys.executable, "-B", str(client), "--server-url", base, "--auth-token", "lan-secret", "--task-file", str(task_file)], capture_output=True, text=True)
            assert client_run.returncode == 0, client_run.stderr + client_run.stdout
            assert json.loads(client_run.stdout)["status"] == "done"

            async_task = build_task(
                task_id="a2a-job-1", target_role="worker", operation="work", target_paths=["target.txt"],
                objective="Run an asynchronous daemon smoke without editing.", acceptance_criteria=["The job reaches done."],
                constraints=["Do not modify files."], inputs=[], goal_id="nightly-smoke", expected_change=False,
            )
            queued_status, queued = post(f"{base}/a2a/jobs", async_task, "lan-secret")
            assert queued_status == 202 and queued["status"] in {"queued", "running", "done"}, queued
            job_id = queued["job_id"]
            for _ in range(30):
                job_request = urllib.request.Request(f"{base}/a2a/jobs/{job_id}", headers={"Authorization": "Bearer lan-secret"})
                with urllib.request.urlopen(job_request) as job_response:
                    job = json.loads(job_response.read())
                if job["status"] == "done":
                    break
                time.sleep(0.2)
            assert job["status"] == "done", job
            reloaded_state = A2AState(Namespace(workspace_root=str(root), auth_token="lan-secret", worker_agent_type=None, verifier_agent_type=None, timeout_seconds=None, state_dir=str(durable_root)))
            assert reloaded_state.get_job(job_id)["status"] == "done"
            goals_request = urllib.request.Request(f"{base}/a2a/goals", headers={"Authorization": "Bearer lan-secret"})
            with urllib.request.urlopen(goals_request) as goals_response:
                goals = json.loads(goals_response.read())
            assert goals["goals"][0]["goal_id"] == "nightly-smoke"
            schedule_body = json.dumps({"schedule_id": "future-smoke", "task": async_task, "run_at": time.time() + 3600}).encode("utf-8")
            schedule_request = urllib.request.Request(f"{base}/a2a/schedules", data=schedule_body, headers={"Content-Type": "application/json", "Authorization": "Bearer lan-secret"}, method="POST")
            with urllib.request.urlopen(schedule_request) as schedule_response:
                assert schedule_response.status == 201
            delete_request = urllib.request.Request(f"{base}/a2a/schedules/future-smoke", headers={"Authorization": "Bearer lan-secret"}, method="DELETE")
            with urllib.request.urlopen(delete_request) as delete_response:
                assert delete_response.status == 200

            os.environ["PATH"] = str(modern_team_fixture) + os.pathsep + old_path
            os.environ["FAKE_TEAM_FILE_PATH"] = str(modern_team_root / "config.json")
            capability_request = urllib.request.Request(f"{base}/capabilities", headers={"Authorization": "Bearer lan-secret"})
            with urllib.request.urlopen(capability_request) as capability_response:
                capability_status = capability_response.status
                capabilities = json.loads(capability_response.read())
            assert capability_status == 200 and capabilities["healthy"] is True
            assert capabilities["claude"]["native_team_tools_available"] is True
            assert capabilities["claude"]["native_team_mode"] == "implicit-agent"
            assert capabilities["claude"]["legacy_team_tools_available"] is False
            team_task = build_task(
                task_id="a2a-team-1",
                target_role="team",
                operation="team",
                target_paths=["target.txt"],
                objective="Run a bounded native Agent Team without editing.",
                acceptance_criteria=["Both native team members return a bounded result."],
                constraints=["Do not modify files."],
                inputs=[],
                team={"name": "requested-name", "members": [
                    {"name": "worker", "role": "worker", "objective": "Return the worker smoke result."},
                    {"name": "reviewer", "role": "verifier", "objective": "Return the verifier smoke result."},
                ]},
                expected_change=False,
            )
            status, team_result = post(f"{base}/a2a/tasks", team_task, "lan-secret")
            assert status == 200 and team_result["status"] == "done", team_result
            assert team_result["target_role"] == "team"
            assert team_result["server_receipt"]["team_mode"] is True
            assert team_result["server_receipt"]["team_complete"] is True
            assert "FAKE_MODERN_TEAM_worker" in team_result["output"]
            assert "FAKE_MODERN_TEAM_reviewer" in team_result["output"]

            os.environ["PATH"] = str(legacy_team_fixture) + os.pathsep + old_path
            os.environ["FAKE_TEAM_FILE_PATH"] = str(legacy_team_root / "config.json")
            legacy_team_task = dict(team_task)
            legacy_team_task["task_id"] = "a2a-legacy-team-1"
            legacy_team_task["context_digest"] = digest_without_context_digest(legacy_team_task)
            status, legacy_team_result = post(f"{base}/a2a/tasks", legacy_team_task, "lan-secret")
            assert status == 200 and legacy_team_result["status"] == "done", legacy_team_result
            assert legacy_team_result["server_receipt"]["native_team_mode"] == "legacy-create-delete"

            verifier = build_task(
                task_id="a2a-verifier-1",
                target_role="verifier",
                operation="verify",
                target_paths=["target.txt"],
                objective="Independently inspect the requested evidence.",
                acceptance_criteria=["No files are changed."],
                constraints=["Read-only."],
                inputs=[],
                expected_change=False,
            )
            status, verify_result = post(f"{base}/a2a/tasks", verifier, "lan-secret")
            assert status == 200 and verify_result["status"] == "done", verify_result

            unauth_status, _ = post(f"{base}/a2a/tasks", task)
            assert unauth_status == 401

            busy_task = dict(task)
            busy_task["task_id"] = "a2a-busy-1"
            busy_task["context_digest"] = digest_without_context_digest(busy_task)
            workspace_lock = state.workspace_locks.setdefault(str(root.resolve()), threading.Lock())
            assert workspace_lock.acquire(blocking=False)
            try:
                busy_status, busy_result = post(f"{base}/a2a/tasks", busy_task, "lan-secret")
                assert busy_status == 400 and "workspace is busy" in busy_result["error"]
            finally:
                workspace_lock.release()

            polluted = dict(task)
            polluted["task_id"] = "polluted"
            polluted["conversation"] = ["must not cross the boundary"]
            polluted["context_digest"] = digest_without_context_digest(polluted)
            try:
                validate_task(polluted)
            except ProtocolError:
                pass
            else:
                raise AssertionError("conversation context was accepted")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            shutil.rmtree(modern_team_root, ignore_errors=True)
            shutil.rmtree(legacy_team_root, ignore_errors=True)
            shutil.rmtree(durable_root, ignore_errors=True)
    test_agent_type_default_omission()
    test_native_capability_fallback()
    print(json.dumps({"protocol": "claude-a2a/0.1", "health": True, "worker": "done", "verifier": "done", "unauthorized": True, "context_boundary": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
