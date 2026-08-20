from evals.codex_baseline import (
    _baseline_prompt,
    resolve_codex_executable,
    usage_from_events,
)
from agent_relay.task import DelegationTask


def test_usage_from_events_sums_turns_and_distinguishes_telemetry() -> None:
    output = "\n".join(
        [
            '{"type":"turn.completed","usage":{"input_tokens":12,"cached_input_tokens":4,"output_tokens":7}}',
            '{"type":"turn.completed","usage":{"input_tokens":3,"output_tokens":2,"reasoning_output_tokens":1}}',
        ]
    )

    assert usage_from_events(output) == {
        "input_tokens": 15,
        "cached_input_tokens": 4,
        "output_tokens": 9,
        "reasoning_output_tokens": 1,
        "turns": 2,
        "token_status": "provider-telemetry",
        "total_tokens": 24,
    }


def test_usage_from_events_does_not_turn_missing_usage_into_zero() -> None:
    assert usage_from_events('{"type":"turn.completed"}') == {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "turns": 0,
        "token_status": "unavailable",
        "total_tokens": None,
    }


def test_baseline_prompt_keeps_write_scope_and_verification_explicit() -> None:
    task = DelegationTask(
        task_id="baseline-task",
        task_kind="mechanical",
        objective="Change one value.",
        allowed_files=("value.py",),
        requirements=("The value is 2.",),
        constraints=("Do not touch tests.",),
        verification=("python -c \"assert True\"",),
    )

    prompt = _baseline_prompt(task, "value.py context")

    assert "Allowed write files:\n- value.py" in prompt
    assert "Do not touch tests." in prompt
    assert "python -c \"assert True\"" in prompt
    assert "value.py context" in prompt


def test_baseline_prompt_carries_ranges_and_insert_mode() -> None:
    task = DelegationTask(
        task_id="baseline-ranged-task",
        task_kind="test_generation",
        objective="Add one focused test.",
        allowed_files=("tests/test_value.py",),
        context=("tests/test_value.py:4-6",),
        context_mode="insert_after",
    )

    prompt = _baseline_prompt(task, "tests/test_value.py:4-6 context")

    assert "tests/test_value.py:4-6" in prompt
    assert "write only inside ranges" in prompt
    assert "insert only the requested new definitions" in prompt


def test_resolve_codex_executable_accepts_explicit_path() -> None:
    assert resolve_codex_executable("codex-test") == "codex-test"
