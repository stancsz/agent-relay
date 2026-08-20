import json
from argparse import Namespace
from pathlib import Path

from agent_relay import cli
from agent_relay.task import DelegationTask
from agent_relay.triage import DelegationDecision, triage_task


def _safe_task(**overrides: object) -> DelegationTask:
    values: dict[str, object] = {
        "task_id": "triage-safe",
        "objective": "Reject negative timeout values while preserving zero and positive values.",
        "allowed_files": ("src/config.py", "tests/test_config.py"),
        "context": ("src/config.py", "tests/test_config.py"),
        "requirements": ("Negative values raise ValueError.",),
        "constraints": ("Make the smallest valid change.",),
        "verification": ("pytest tests/test_config.py",),
        "success_criteria": ("The declared test passes.",),
        "task_kind": "bounded_bugfix",
        "risk_flags": (),
    }
    values.update(overrides)
    return DelegationTask(**values)


def test_safe_task_delegates_only_with_positive_token_leverage() -> None:
    result = triage_task(
        _safe_task(),
        expected_codex_tokens_avoided=1800,
        expected_codex_tokens_spent=600,
    )

    assert result.decision is DelegationDecision.DELEGATE
    assert result.confidence.value == "HIGH"
    assert result.leverage == 3.0
    assert result.net_savings_rate == 2 / 3
    assert not result.reason_codes


def test_missing_task_kind_is_blocked_before_model_invocation() -> None:
    result = triage_task(
        _safe_task(task_kind="unspecified"),
        expected_codex_tokens_avoided=1800,
        expected_codex_tokens_spent=600,
    )

    assert result.decision is DelegationDecision.BLOCKED
    assert "task_kind_required" in result.reason_codes


def test_missing_deterministic_verification_is_blocked() -> None:
    result = triage_task(
        _safe_task(verification=()),
        expected_codex_tokens_avoided=1800,
        expected_codex_tokens_spent=600,
    )

    assert result.decision is DelegationDecision.BLOCKED
    assert "deterministic_verification_required" in result.reason_codes


def test_security_language_keeps_task_in_parent() -> None:
    result = triage_task(
        _safe_task(
            objective="Add authentication validation for expired credentials.",
            task_kind="bounded_bugfix",
        ),
        expected_codex_tokens_avoided=4000,
        expected_codex_tokens_spent=500,
    )

    assert result.decision is DelegationDecision.KEEP_LOCAL
    assert "security" in result.risk_flags
    assert "credentials" in result.risk_flags
    assert "risk_flags_present" in result.reason_codes


def test_broad_scope_and_unpriced_economics_do_not_delegate() -> None:
    result = triage_task(
        _safe_task(allowed_files=("a.py", "b.py", "c.py", "d.py")),
    )

    assert result.decision is DelegationDecision.KEEP_LOCAL
    assert "write_scope_too_broad" in result.reason_codes
    assert "economics_unpriced" in result.reason_codes


def test_cli_triage_returns_machine_readable_recommendation(
    tmp_path: Path,
    capsys,
) -> None:
    task_path = tmp_path / "task.json"
    task_path.write_text(
        json.dumps(_safe_task().to_dict()),
        encoding="utf-8",
    )
    args = Namespace(
        task=task_path,
        avoided_tokens=1800,
        spent_tokens=600,
        minimum_leverage=2.0,
        max_files=3,
        max_context_items=6,
        as_json=True,
    )

    assert cli._triage(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"] == "DELEGATE"
    assert payload["gates"]["deterministic_verification"] is True


def test_cli_main_triage_uses_nonzero_exit_for_keep_local(
    tmp_path: Path,
    capsys,
) -> None:
    task_path = tmp_path / "task.json"
    task_path.write_text(
        json.dumps(_safe_task(task_kind="security").to_dict()),
        encoding="utf-8",
    )

    assert cli.main([
        "triage",
        "--task",
        str(task_path),
        "--avoided-tokens",
        "1800",
        "--spent-tokens",
        "600",
    ]) == 1
    assert json.loads(capsys.readouterr().out)["decision"] == "KEEP_LOCAL"
