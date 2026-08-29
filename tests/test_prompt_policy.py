from __future__ import annotations

import sys
from pathlib import Path

from agent_relay.acceptance import build_candidate_review_prompt
from agent_relay.agy_antigravity import build_agy_prompt
from agent_relay.agent_invocation import _policy_prompt
from agent_relay.claude_mcp import _prompt as build_claude_mcp_prompt
from agent_relay.claude_task import remote_collaboration_contract
from agent_relay.codex_review import build_review_prompt
from agent_relay.codex_worker import build_codex_prompt
from agent_relay.prompt_policy import HIGH_AGENCY_GUIDANCE, PROMPT_POLICY_VERSION, with_high_agency_guidance
from agent_relay.result import DelegationResult, ResultStatus, VerificationResult
from agent_relay.task import DelegationTask
from agent_relay.worker import SYSTEM_PROMPT
from evals.codex_baseline import _baseline_prompt

LANE_SCRIPTS = Path(__file__).resolve().parents[1] / "lanes" / "claude-task" / "scripts"
if str(LANE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(LANE_SCRIPTS))

from claude_a2a_server import render_prompt  # noqa: E402
from claude_mcp_delegate import render_member_prompt  # noqa: E402
from prompt_policy import (  # noqa: E402
    HIGH_AGENCY_GUIDANCE as STANDALONE_GUIDANCE,
    PROMPT_POLICY_VERSION as STANDALONE_VERSION,
)
from a2a_protocol import build_task  # noqa: E402


def _task() -> DelegationTask:
    return DelegationTask(
        task_id="prompt-policy",
        task_kind="bounded_bugfix",
        objective="Change one bounded value.",
        allowed_files=("value.py",),
        context=("value.py",),
        requirements=("The value is two.",),
        constraints=("Do not touch unrelated files.",),
        verification=("python -c \"assert True\"",),
        success_criteria=("The focused check passes.",),
    )


def _assert_guidance_at_start(prompt: str) -> None:
    assert prompt.startswith(HIGH_AGENCY_GUIDANCE)
    assert "Explore first" in prompt
    assert "materially affects safety, authorization, scope, or acceptance" in prompt
    assert "do not append optional follow-up questions" in prompt
    assert "reporting slot, not an invitation" in prompt
    assert "bounded alternatives" in prompt
    assert "observed fact -> cause or decision -> fix -> verification" in prompt


def test_shared_guidance_is_idempotent_and_matches_standalone_lane_copy() -> None:
    assert with_high_agency_guidance(HIGH_AGENCY_GUIDANCE) == HIGH_AGENCY_GUIDANCE
    assert STANDALONE_GUIDANCE == HIGH_AGENCY_GUIDANCE
    assert PROMPT_POLICY_VERSION == "1.0.0"
    assert STANDALONE_VERSION == PROMPT_POLICY_VERSION


def test_runtime_prompt_surfaces_start_with_high_agency_guidance() -> None:
    task = _task()
    result = DelegationResult(
        task_id=task.task_id,
        status=ResultStatus.SUCCESS,
        patch="diff --git a/value.py b/value.py\n",
        verification=(VerificationResult("python -c \"assert True\"", 0),),
    )
    prompts = [
        SYSTEM_PROMPT,
        _policy_prompt("Inspect the bounded task.", workdir=Path.cwd(), mode="read-only"),
        build_codex_prompt(task, "value.py context"),
        _baseline_prompt(task, "value.py context"),
        build_agy_prompt("Inspect the bounded task."),
        build_claude_mcp_prompt(task),
        build_review_prompt(),
        build_candidate_review_prompt(task, result),
        "",  # replaced below after the standalone task packet is built
    ]
    packet = build_task(
        task_id="a2a-prompt-policy",
        target_role="worker",
        operation="work",
        target_paths=["value.py"],
        objective="Inspect one bounded target.",
        acceptance_criteria=["Return a bounded result."],
        constraints=["Do not modify unrelated files."],
        inputs=[],
    )
    prompts[-1] = render_prompt(packet)
    prompts.append(
        render_member_prompt(
            {
                "team_name": "prompt-policy-team",
                "shared": {
                    "task_id": "team-prompt-policy",
                    "context_digest": "0" * 64,
                    "objective": "Inspect one bounded target.",
                    "target_paths": ["value.py"],
                    "acceptance_criteria": ["Return a bounded result."],
                    "constraints": ["Do not modify unrelated files."],
                    "inputs": [],
                    "profile_context": {},
                },
            },
            {"name": "worker", "role": "worker", "objective": "Inspect the target."},
        )
    )
    for prompt in prompts:
        _assert_guidance_at_start(prompt)
    assert build_agy_prompt(HIGH_AGENCY_GUIDANCE + "\n\nInspect the bounded task.").count(
        HIGH_AGENCY_GUIDANCE
    ) == 1


def test_remote_question_contract_requires_exploration_before_asking() -> None:
    contract = remote_collaboration_contract()
    assert any("inspect all declared inputs" in item for item in contract["before_edit"])
    assert any("bounded alternative" in item for item in contract["before_edit"])
    assert contract["question_policy"].startswith("After relevant exploration")
