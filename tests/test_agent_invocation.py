from __future__ import annotations

from pathlib import Path

import pytest

import agent_relay.agent_invocation as invocation
from agent_relay.agent_invocation import AgentInvocationConfig, AgentInvoker


def config(tmp_path: Path, **kwargs) -> AgentInvocationConfig:
    values = {
        "workspace_root": tmp_path,
        "timeout_seconds": 10,
        "max_output_chars": 4_000,
        "max_concurrency": 1,
    }
    values.update(kwargs)
    return AgentInvocationConfig(**values)


def test_auto_gemini_prefers_usable_agy_when_direct_project_is_not_configured(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("AR_GEMINI_BIN", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT_ID", raising=False)

    def fake_which(name: str) -> str | None:
        return "C:/tools/agy.exe" if name == "agy.exe" else None

    monkeypatch.setattr(invocation.shutil, "which", fake_which)
    selected = invocation._gemini_transport(config(tmp_path))
    assert selected == ("agy", "C:/tools/agy.exe")


def test_gemini_agy_invocation_returns_normalized_result(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        invocation,
        "_gemini_transport",
        lambda _config: ("agy", "agy-test"),
    )
    captured = {}

    def fake_run_agy(repo, prompt, *, config):
        captured.update({"repo": repo, "prompt": prompt, "config": config})
        return type(
            "FakeAgyResult",
            (),
            {
                "status": "PASS",
                "summary": "agy completed",
                "response": "AGY_OK",
                "return_code": 0,
                "duration_seconds": 0.2,
                "runtime": {"read_only_default": True},
            },
        )()

    monkeypatch.setattr(invocation, "run_agy", fake_run_agy)
    result = AgentInvoker(config(tmp_path)).invoke("gemini", "Return a bounded answer.")
    assert result.passed
    assert result.transport == "agy"
    assert result.response == "AGY_OK"
    assert captured["repo"] == tmp_path.resolve()
    assert captured["config"].mode == "plan"


def test_codex_invocation_uses_ephemeral_read_only_sandbox(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(invocation, "_codex_executable", lambda _config: "codex-test")
    captured = {}

    def fake_run_command(**kwargs):
        captured.update(kwargs)
        return invocation.AgentInvocationResult(
            agent="codex",
            transport="codex-cli",
            status="PASS",
            summary="codex completed",
            response="CODEX_OK",
            return_code=0,
            duration_seconds=0.1,
            runtime={},
        )

    monkeypatch.setattr(invocation, "_run_command", fake_run_command)
    result = AgentInvoker(config(tmp_path)).invoke(
        "codex", "Review the bounded code.", model="gpt-test"
    )
    assert result.response == "CODEX_OK"
    command = captured["command"]
    assert command[:2] == ["codex-test", "exec"]
    assert "--sandbox" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" in command
    assert command[command.index("--model") + 1] == "gpt-test"
    assert command[-1] == "-"
    assert "Review the bounded code." in captured["stdin_text"]
    assert captured["workdir"] == tmp_path.resolve()


def test_gemini_direct_invocation_uses_stdin_prompt(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        invocation,
        "_gemini_transport",
        lambda _config: ("gemini", "gemini-test"),
    )
    captured = {}

    def fake_run_command(**kwargs):
        captured.update(kwargs)
        return invocation.AgentInvocationResult(
            agent="gemini",
            transport="gemini",
            status="PASS",
            summary="gemini completed",
            response="GEMINI_OK",
            return_code=0,
            duration_seconds=0.1,
            runtime={},
        )

    monkeypatch.setattr(invocation, "_run_command", fake_run_command)
    result = AgentInvoker(config(tmp_path)).invoke("gemini", "Inspect without editing.")
    assert result.response == "GEMINI_OK"
    assert "Inspect without editing." not in captured["command"]
    assert "Inspect without editing." in captured["stdin_text"]
    assert captured["command"][0:2] == ["gemini-test", "--output-format"]


def test_claude_invocation_limits_read_only_tools(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(invocation, "_claude_executable", lambda _config: "claude-test")
    captured = {}

    def fake_run_command(**kwargs):
        captured.update(kwargs)
        return invocation.AgentInvocationResult(
            agent="claude",
            transport="claude-cli",
            status="PASS",
            summary="claude completed",
            response="CLAUDE_OK",
            return_code=0,
            duration_seconds=0.1,
            runtime={},
        )

    monkeypatch.setattr(invocation, "_run_command", fake_run_command)
    AgentInvoker(config(tmp_path)).invoke("claude", "Inspect the bounded code.")
    command = captured["command"]
    assert command[0:2] == ["claude-test", "--print"]
    assert "Inspect the bounded code." not in command
    assert "Inspect the bounded code." in captured["stdin_text"]
    assert command[command.index("--permission-mode") + 1] == "plan"
    assert command[command.index("--allowed-tools") + 1] == "Read"
    assert "--no-session-persistence" in command


def test_workspace_write_requires_explicit_server_opt_in(tmp_path) -> None:
    with pytest.raises(ValueError, match="workspace-write is disabled"):
        AgentInvoker(config(tmp_path)).invoke(
            "codex", "Make the bounded edit.", mode="workspace-write"
        )


def test_workspace_write_is_allowed_only_inside_root(monkeypatch, tmp_path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    monkeypatch.setattr(invocation, "_codex_executable", lambda _config: "codex-test")
    monkeypatch.setattr(
        invocation,
        "_run_command",
        lambda **kwargs: invocation.AgentInvocationResult(
            agent="codex",
            transport="codex-cli",
            status="PASS",
            summary="ok",
            response="OK",
            return_code=0,
            duration_seconds=0,
            runtime=kwargs,
        ),
    )
    result = AgentInvoker(config(tmp_path, allow_workspace_writes=True)).invoke(
        "codex", "Make the bounded edit.", workdir="nested", mode="workspace-write"
    )
    assert result.passed
    with pytest.raises(ValueError, match="inside the configured"):
        AgentInvoker(config(tmp_path, allow_workspace_writes=True)).invoke(
            "codex", "Do not escape.", workdir=tmp_path.parent, mode="workspace-write"
        )


def test_response_parser_handles_codex_jsonl_and_plain_text() -> None:
    output = '{"type":"item.completed","item":{"type":"agent_message","text":"final"}}\n'
    assert invocation._response_from_output(output) == "final"
    assert invocation._response_from_output("plain response\n") == "plain response"


def test_bounded_output_includes_marker_inside_the_configured_limit() -> None:
    bounded, truncated = invocation._bounded("x" * 100, 40)
    assert truncated
    assert len(bounded) == 40
    assert bounded.endswith("[agent output truncated]")
