from agent_relay.codex_review import (
    CodexReviewConfig,
    build_review_prompt,
    run_codex_review,
)
from agent_relay.agy_antigravity import AgyConfig, run_agy
import subprocess
from agent_relay.lanes import canonical_lane_name, lane_manifest


def test_lane_registry_uses_canonical_names_and_roles() -> None:
    manifest = lane_manifest()
    assert [item["name"] for item in manifest] == [
        "local-qwen",
        "claude-task",
        "codex-review",
        "agy-antigravity",
    ]
    assert manifest[0]["role"] == "mechanical worker"
    assert manifest[1]["role"] == "primary implementation/team worker"
    assert manifest[2]["role"] == "independent verifier"
    assert manifest[2]["model"] == "gpt-5.6-sol"
    assert manifest[-1]["role"] == "Google-stack scout/planner"
    assert canonical_lane_name("codex-ollama") == "local-qwen"
    assert canonical_lane_name("review") == "codex-review"
    assert canonical_lane_name("agy") == "agy-antigravity"


def test_review_prompt_is_read_only_and_customizable() -> None:
    prompt = build_review_prompt("Focus on missing regression tests.")
    assert "read-only" in prompt
    assert "Do not edit files" in prompt
    assert "missing regression tests" in prompt


def test_review_config_defaults_to_subscription_verifier() -> None:
    config = CodexReviewConfig.from_env(executable="codex-test")
    assert config.model == "gpt-5.6-sol"
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


def test_agy_config_defaults_to_google_specialist() -> None:
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
