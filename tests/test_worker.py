import json

import pytest

from agent_relay.task import DelegationTask
from agent_relay.worker import RetryEvidence, build_prompt, parse_worker_response


def test_parse_worker_json_with_markdown_fence() -> None:
    value = {
        "status": "READY",
        "summary": "changed one line",
        "patch": "diff --git a/config.py b/config.py\n--- a/config.py\n+++ b/config.py\n",
        "blockers": [],
    }
    fence = chr(96) * 3
    response = parse_worker_response(fence + "json\n" + json.dumps(value) + "\n" + fence)
    assert response.status == "READY"
    assert response.patch.startswith("diff --git")


def test_parse_raw_diff() -> None:
    response = parse_worker_response(
        "diff --git a/config.py b/config.py\n"
        "--- a/config.py\n"
        "+++ b/config.py\n"
    )
    assert response.status == "READY"


def test_parse_complete_file_response() -> None:
    response = parse_worker_response(json.dumps({
        "status": "READY",
        "summary": "replaced file",
        "patch": "",
        "files": {"config.py": "VALUE = 2\n"},
        "blockers": [],
    }))
    assert response.file_contents == (("config.py", "VALUE = 2\n"),)


def test_files_output_wins_over_placeholder_patch() -> None:
    response = parse_worker_response(json.dumps({
        "status": "READY",
        "patch": "- config.py\n+ config.py",
        "files": {"config.py": "VALUE = 2\n"},
        "blockers": [],
    }))
    assert response.patch == ""
    assert response.file_contents == (("config.py", "VALUE = 2\n"),)


def test_parse_truncated_outer_json_response() -> None:
    response = parse_worker_response(
        '{"status":"READY","files":{"config.py":"VALUE = 2\\n"}'
    )
    assert response.file_contents == (("config.py", "VALUE = 2\n"),)


def test_parse_legacy_file_contents_when_files_is_empty() -> None:
    response = parse_worker_response(json.dumps({
        "status": "READY",
        "files": {},
        "file_contents": {"config.py": "VALUE = 3\n"},
    }))
    assert response.file_contents == (("config.py", "VALUE = 3\n"),)


def test_reject_json_with_trailing_garbage() -> None:
    with pytest.raises(ValueError):
        parse_worker_response('{"status":"READY","patch":"diff"} trailing')


def test_parse_blocked_response() -> None:
    response = parse_worker_response(
        json.dumps({
            "status": "BLOCKED",
            "summary": "missing requirements",
            "patch": "",
            "blockers": ["ambiguous objective"],
        })
    )
    assert response.blocked
    assert response.blockers == ("ambiguous objective",)


def test_reject_invalid_worker_output() -> None:
    with pytest.raises(ValueError):
        parse_worker_response("not a patch and not JSON")


def test_ranged_retry_requires_a_unified_diff() -> None:
    task = DelegationTask(
        task_id="ranged-retry",
        objective="set value",
        allowed_files=("value.py",),
        context=("value.py:1-1",),
    )
    prompt = build_prompt(
        task,
        "[value.py lines 1-1]\nVALUE = 1",
        RetryEvidence(
            previous_patch="partial replacement",
            verification=(),
            failure_summary="verification failed",
        ),
    )
    assert "minimal unified diff or only" in prompt
    assert "do not return a complete replacement file" in prompt


def test_retry_prompt_bounds_large_evidence() -> None:
    task = DelegationTask(
        task_id="retry-size",
        objective="set value",
        allowed_files=("value.py",),
    )
    prompt = build_prompt(
        task,
        "VALUE = 1",
        RetryEvidence(
            previous_patch="x" * 100_000,
            verification=({
                "command": "pytest -q",
                "exit_code": 1,
                "stdout": "y" * 100_000,
                "stderr": "z" * 100_000,
                "duration_seconds": 0.1,
                "timed_out": False,
                "passed": False,
            },),
            failure_summary="failure",
        ),
    )
    assert len(prompt) < 20_000
    assert "retry evidence truncated" in prompt
