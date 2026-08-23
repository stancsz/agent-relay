from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from agent_relay.acceptance import build_candidate_review_prompt, enforce_acceptance
from agent_relay.codex_review import CodexReviewResult
from agent_relay.result import DelegationResult, ResultStatus, VerificationResult
from agent_relay.task import DelegationTask


def _task() -> DelegationTask:
    return DelegationTask(
        task_id="acceptance-task",
        objective="Change one bounded value.",
        allowed_files=("value.py",),
        verification=("python -c \"assert True\"",),
        task_kind="mechanical",
    )


def _result(*, verification: tuple[VerificationResult, ...] = ()) -> DelegationResult:
    return DelegationResult(
        task_id="acceptance-task",
        status=ResultStatus.SUCCESS,
        summary="candidate",
        files_changed=("value.py",),
        patch="diff --git a/value.py b/value.py\n+VALUE = 2\n",
        verification=verification,
        metadata={"lane": "claude-task", "main_worktree_unchanged": True},
    )


def _passing_review(**_kwargs) -> CodexReviewResult:
    return CodexReviewResult(
        status="PASS",
        summary="No actionable findings.",
        findings="No actionable findings.",
        return_code=0,
        duration_seconds=0.1,
        runtime={"model": "gpt-5.6-sol"},
    )


def test_acceptance_blocks_missing_deterministic_evidence(tmp_path: Path) -> None:
    called = False

    def unexpected_review(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("Sol must not review an unverified candidate")

    result = enforce_acceptance(
        _task(),
        tmp_path,
        _result(),
        review_runner=unexpected_review,
    )

    assert result.status is ResultStatus.FAILED_VERIFICATION
    assert "deterministic verification evidence is missing" in result.blockers
    assert called is False


def test_acceptance_requires_sol_and_records_both_gates(tmp_path: Path) -> None:
    calls = []

    def fake_review(repo, **kwargs):
        calls.append((repo, kwargs))
        return _passing_review(**kwargs)

    result = enforce_acceptance(
        _task(),
        tmp_path,
        _result(verification=(VerificationResult("pytest -q", 0),)),
        review_runner=fake_review,
    )

    assert result.status is ResultStatus.SUCCESS
    assert result.metadata["acceptance_gates"] == [
        "deterministic-verification",
        "sol-reviewer",
    ]
    assert result.metadata["sol_review"]["runtime"]["model"] == "gpt-5.6-sol"
    assert calls[0][1]["uncommitted"] is False
    assert calls[0][1]["config"].model == "gpt-5.6-sol"
    assert calls[0][1]["config"].reasoning_effort == "high"
    assert "Candidate patch" in calls[0][1]["prompt"]


def test_sol_prompt_contains_bounded_remote_context_and_handoff() -> None:
    result = _result(verification=(VerificationResult("pytest -q", 0),))
    result = replace(
        result,
        metadata={
            **result.metadata,
            "transport": "remote-claude-a2a",
            "remote_endpoint": "https://pc-b.example.test/a2a",
            "verification_authority": "parent-local-sandbox",
            "claude_packet_context_digest": "a" * 64,
            "context_inputs": [{
                "path": "value.py",
                "sha256": "b" * 64,
                "excerpt": "VALUE = 1",
            }],
            "collaboration_contract": {
                "mode": "bounded-remote",
                "context_policy": "declared-inputs-and-target-paths-only",
                "before_edit": ["state assumptions"],
                "handoff_fields": ["questions_for_orchestrator", "recommended_next_prompt"],
                "question_policy": "ask exact questions",
            },
            "remote_worker_handoff": "questions_for_orchestrator: none\nrecommended_next_prompt: run pytest",
            "progress_evidence": {
                "required": True,
                "satisfied": True,
                "missing_steps": [],
                "blocked_steps": [],
                "evidence": {"verify": {"status": "done", "evidence": "pytest exit_code=0"}},
            },
        },
    )
    prompt = build_candidate_review_prompt(_task(), result)

    assert "Constraints:" in prompt
    assert "Declared verification commands:" in prompt
    assert "value.py (sha256" in prompt
    assert "bounded-remote" in prompt
    assert "questions_for_orchestrator" in prompt
    assert "parent-local-sandbox" in prompt
    assert "recommended_next_prompt: run pytest" in prompt
    assert "Parsed handoff fields" in prompt
    assert "Required babystep progress/evidence receipt" in prompt
    assert "pytest exit_code=0" in prompt


def test_acceptance_fails_closed_when_sol_rejects(tmp_path: Path) -> None:
    result = enforce_acceptance(
        _task(),
        tmp_path,
        _result(verification=(VerificationResult("pytest -q", 0),)),
        review_runner=lambda _repo, **_kwargs: CodexReviewResult(
            status="FAILED",
            summary="Sol found a correctness defect",
            findings="Defect",
            return_code=1,
            duration_seconds=0.1,
            runtime={"model": "gpt-5.6-sol"},
        ),
    )

    assert result.status is ResultStatus.BLOCKED
    assert "Sol found a correctness defect" in result.blockers
    assert result.metadata["sol_review"]["status"] == "FAILED"
