from pathlib import Path
import tomllib

from agent_relay import env as env_module
from agent_relay.codex_review import (
    CodexReviewConfig,
    build_review_prompt,
    run_codex_review,
)
from agent_relay.agy_antigravity import AgyConfig, run_agy
import subprocess
from agent_relay.lanes import canonical_lane_name, lane_health_manifest, lane_manifest
from agent_relay import lanes as lanes_module


def test_lane_registry_uses_canonical_names_and_roles() -> None:
    manifest = lane_manifest()
    assert [item["name"] for item in manifest] == [
        "local-qwen",
        "claude-task",
        "claude-mcp",
        "codex-review",
        "agy-antigravity",
    ]
    assert manifest[0]["role"] == "mechanical worker"
    assert manifest[1]["role"] == "primary implementation/team worker"
    assert manifest[3]["role"] == "independent verifier"
    assert manifest[3]["model"] == "gpt-5.6-luna"
    assert manifest[-1]["role"] == "Google-stack scout/planner"
    assert canonical_lane_name("codex-ollama") == "local-qwen"
    assert canonical_lane_name("review") == "codex-review"
    assert canonical_lane_name("agy") == "agy-antigravity"


def test_lane_manifest_reads_subagent_model_overrides(monkeypatch) -> None:
    monkeypatch.setenv("AR_CODEX_MODEL", "qwen-ar")
    monkeypatch.setenv("AR_CODEX_REVIEW_MODEL", "gpt-review-ar")
    monkeypatch.setenv("AR_AGY_MODEL", "gemini-ar")
    manifest = lane_manifest()
    assert manifest[0]["model"] == "qwen-ar"
    assert manifest[3]["model"] == "gpt-review-ar"
    assert manifest[-1]["model"] == "gemini-ar"


def test_lane_manifest_respects_home_dotenv_values(monkeypatch, tmp_path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "\n".join(
            [
                "AR_CODEX_MODEL=qwen-dotenv",
                "AR_CODEX_REVIEW_MODEL=gpt-review-dotenv",
                "AR_AGY_MODEL=gemini-dotenv",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(env_module.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("AR_CODEX_MODEL", raising=False)
    monkeypatch.delenv("AR_CODEX_REVIEW_MODEL", raising=False)
    monkeypatch.delenv("AR_AGY_MODEL", raising=False)
    env_module.load_dotenv(path=dotenv, force=True)

    manifest = lane_manifest()
    assert manifest[0]["model"] == "qwen-dotenv"
    assert manifest[3]["model"] == "gpt-review-dotenv"
    assert manifest[-1]["model"] == "gemini-dotenv"


def test_lane_health_manifest_reports_missing_prerequisites(monkeypatch) -> None:
    monkeypatch.setattr(lanes_module.shutil, "which", lambda _name: None)
    for name in ("AR_CODEX_BIN", "AR_CLAUDE_BIN", "AR_AGY_BIN"):
        monkeypatch.delenv(name, raising=False)

    manifest = lane_health_manifest(probe=False)

    assert {item["name"] for item in manifest} == {
        "local-qwen",
        "claude-task",
        "claude-mcp",
        "codex-review",
        "agy-antigravity",
    }
    assert all(item["status"] == "blocked" for item in manifest if item["name"] != "claude-mcp")
    assert next(item for item in manifest if item["name"] == "claude-mcp")["status"] == "unknown"
    assert all("health" in item for item in manifest)


def test_lane_health_manifest_reports_available_cli_prerequisites(monkeypatch) -> None:
    def fake_which(name: str) -> str | None:
        return f"C:/tools/{name}" if name in {"codex", "ollama", "claude", "agy"} else None

    monkeypatch.setattr(lanes_module.shutil, "which", fake_which)
    for name in ("AR_CODEX_BIN", "AR_CLAUDE_BIN", "AR_AGY_BIN"):
        monkeypatch.delenv(name, raising=False)

    manifest = lane_health_manifest(probe=False)

    assert all(item["status"] == "unknown" for item in manifest)
    assert {item["health"]["transport"] for item in manifest} == {
        "codex-cli + ollama",
        "claude-a2a-ephemeral",
        "streamable-http-mcp",
        "codex-cli",
        "agy-cli",
    }


def test_reviewer_subagent_toml_defaults_to_gpt_56_luna() -> None:
    reviewer = tomllib.loads(
        Path(".codex/agents/reviewer.toml").read_text(encoding="utf-8")
    )
    assert reviewer["model"] == "gpt-5.6-luna"


def test_review_prompt_is_read_only_and_customizable() -> None:
    prompt = build_review_prompt("Focus on missing regression tests.")
    assert "read-only" in prompt
    assert "Do not edit files" in prompt
    assert "missing regression tests" in prompt


def test_review_config_reads_agent_relay_environment(monkeypatch) -> None:
    monkeypatch.setenv("AR_CODEX_BIN", "codex-ar")
    monkeypatch.setenv("AR_CODEX_REVIEW_MODEL", "gpt-review-ar")
    monkeypatch.setenv("AR_CODEX_REVIEW_REASONING_EFFORT", "medium")
    monkeypatch.setenv("AR_CODEX_REVIEW_TIMEOUT_SECONDS", "17")

    config = CodexReviewConfig.from_env()

    assert config.executable == "codex-ar"
    assert config.model == "gpt-review-ar"
    assert config.reasoning_effort == "medium"
    assert config.timeout_seconds == 17


def test_review_config_defaults_to_subscription_verifier(monkeypatch) -> None:
    monkeypatch.delenv("AR_CODEX_REVIEW_MODEL", raising=False)
    monkeypatch.delenv("AR_CODEX_REVIEW_REASONING_EFFORT", raising=False)
    config = CodexReviewConfig.from_env(executable="codex-test")
    assert config.model == "gpt-5.6-luna"
    assert config.reasoning_effort == "high"


def test_review_adapter_keeps_cli_failure_as_explicit_receipt(tmp_path) -> None:
    result = run_codex_review(
        tmp_path,
        config=CodexReviewConfig(
            executable="definitely-missing-codex",
            model="gpt-5.6-sol",
            reasoning_effort="high",
            timeout_seconds=1,
        ),
    )
    assert result.status == "FAILED"


def test_review_adapter_uses_selector_without_conflicting_positional_prompt(
    tmp_path, monkeypatch
) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"type":"item.completed","item":{"type":"agent_message","text":"No findings."}}\n',
            stderr="",
        )

    monkeypatch.setattr("agent_relay.codex_review.subprocess.run", fake_run)
    result = run_codex_review(
        tmp_path,
        prompt="Focus on regression tests.",
        config=CodexReviewConfig(
            executable="codex-test",
            model="gpt-5.6-sol",
            reasoning_effort="high",
            timeout_seconds=1,
        ),
    )

    assert result.passed
    command, kwargs = calls[0]
    assert "--uncommitted" in command
    assert "Focus on regression tests." not in command
    assert 'model_reasoning_effort="high"' in command
    assert kwargs["cwd"] == tmp_path.resolve()


def test_agy_config_reads_agent_relay_environment(monkeypatch) -> None:
    monkeypatch.setenv("AR_AGY_BIN", "agy-ar")
    monkeypatch.setenv("AR_AGY_MODEL", "gemini-ar")
    monkeypatch.setenv("AR_AGY_EFFORT", "medium")
    monkeypatch.setenv("AR_AGY_MODE", "accept-edits")
    monkeypatch.setenv("AR_AGY_SANDBOX", "false")
    monkeypatch.setenv("AR_AGY_TIMEOUT_SECONDS", "19")

    config = AgyConfig.from_env()

    assert config.executable == "agy-ar"
    assert config.model == "gemini-ar"
    assert config.effort == "medium"
    assert config.mode == "accept-edits"
    assert config.sandbox is False
    assert config.timeout_seconds == 19


def test_agy_config_defaults_to_google_specialist(monkeypatch) -> None:
    monkeypatch.delenv("AR_AGY_MODEL", raising=False)
    monkeypatch.delenv("AR_AGY_EFFORT", raising=False)
    monkeypatch.delenv("AR_AGY_MODE", raising=False)
    monkeypatch.delenv("AR_AGY_SANDBOX", raising=False)
    config = AgyConfig.from_env(executable="agy-test")
    assert config.model == "gemini-3.1-pro-high"
    assert config.effort == "high"
    assert config.mode == "plan"
    assert config.sandbox is True


def test_agy_adapter_is_plan_mode_by_default(tmp_path, monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"response":"Firebase and Android checks identified."}\n',
            stderr="",
        )

    monkeypatch.setattr("agent_relay.agy_antigravity.subprocess.run", fake_run)
    result = run_agy(
        tmp_path,
        "Review the Google-specific integration risks.",
        config=AgyConfig(
            executable="agy-test",
            model="gemini-3.1-pro-high",
            effort="high",
            mode="plan",
            sandbox=True,
            timeout_seconds=1,
        ),
    )

    assert result.passed
    assert "Firebase" in result.response
    command, kwargs = calls[0]
    assert "--plan" not in command
    assert "--mode" in command and "plan" in command
    assert "--sandbox" in command
    assert kwargs["cwd"] == tmp_path.resolve()
