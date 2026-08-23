import json

import pytest

from agent_relay.escalation import (
    EscalationPolicy,
    EscalationPolicyError,
    load_policy,
    normalize_stage,
)


def test_default_policy_summons_sol_at_plan_end_and_review_end() -> None:
    policy = EscalationPolicy.default()

    plan = policy.evaluate("plan_end", {"task_kind": "mechanical"})
    review = policy.evaluate("review_end", {"verification_failed": False})

    assert plan.action == "consult"
    assert plan.profile is not None
    assert plan.profile.model == "gpt-5.6-sol"
    assert plan.profile.role == "planner"
    assert plan.max_revisions == 1
    assert plan.on_reject == "revise"
    assert plan.on_exhausted == "human_review"
    assert review.action == "require_review"
    assert review.profile is not None
    assert review.profile.model == "gpt-5.6-sol"
    assert review.profile.role == "verifier"
    assert review.max_revisions == 1


def test_stage_aliases_are_canonical() -> None:
    assert normalize_stage("plan") == "plan_end"
    assert normalize_stage("review") == "review_end"


def test_rules_are_priority_ordered_and_match_operational_signals() -> None:
    policy = EscalationPolicy({
        "version": 1,
        "enabled": True,
        "default_action": "continue",
        "profiles": {
            "planner": {"lane": "codex-escalation", "model": "planner-model", "role": "planner"},
            "verifier": {"lane": "codex-review", "model": "verifier-model", "role": "verifier"},
        },
        "rules": [
            {
                "id": "generic",
                "priority": 100,
                "stages": ["recovery"],
                "action": "consult",
                "profile": "planner",
            },
            {
                "id": "failed-proof",
                "priority": 200,
                "stages": ["recovery"],
                "any": {"verification_failed": True, "attempts_at_least": 2},
                "action": "require_review",
                "profile": "verifier",
                "evidence_required": ["failure_history"],
            },
        ],
    })

    decision = policy.evaluate("recovery", {"verification_failed": True, "attempts": 2})
    assert decision.rule_id == "failed-proof"
    assert decision.action == "require_review"
    assert decision.evidence_required == ("failure_history",)


def test_disabled_policy_continues_without_summoning_model() -> None:
    policy = EscalationPolicy({
        "version": 1,
        "enabled": False,
        "default_action": "block",
        "profiles": {},
        "rules": [],
    })

    decision = policy.evaluate("review_end")
    assert decision.action == "continue"
    assert decision.rule_id == "disabled"
    assert decision.reasons == ("policy_disabled",)


def test_malformed_policy_fails_closed() -> None:
    with pytest.raises(EscalationPolicyError, match="unknown escalation stage"):
        EscalationPolicy({
            "version": 1,
            "profiles": {},
            "rules": [{"id": "bad", "stages": ["never"], "action": "continue"}],
        })

    with pytest.raises(EscalationPolicyError, match="unknown profile"):
        EscalationPolicy({
            "version": 1,
            "profiles": {},
            "rules": [{"id": "bad", "stages": ["plan_end"], "action": "consult", "profile": "missing"}],
        })


def test_policy_file_is_loaded_and_decision_is_json_safe(tmp_path) -> None:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "version": 1,
        "enabled": True,
        "default_action": "continue",
        "profiles": {"p": {"lane": "codex-escalation", "model": "sol", "role": "planner"}},
        "rules": [{
            "id": "ambiguous-plan",
            "priority": 500,
            "stages": ["plan_end"],
            "any": {"ambiguity": True},
            "action": "consult",
            "profile": "p",
        }],
    }), encoding="utf-8")

    decision = load_policy(path).evaluate("plan_end", {"ambiguity": True})
    payload = decision.to_dict()
    assert payload["action"] == "consult"
    assert payload["profile"]["model"] == "sol"
    json.dumps(payload)
