"""Configurable high-intelligence escalation policy.

The policy is deliberately pure: it observes a stage and bounded operational
signals, then returns a machine-readable decision. It never invokes a model,
changes a task contract, or treats model confidence as evidence. Callers own
the consultation/review side effect and must record the returned decision.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping


POLICY_VERSION = 1
STAGES = frozenset({"plan_end", "execute", "review_end", "recovery", "release"})
STAGE_ALIASES = {"plan": "plan_end", "review": "review_end"}
ACTIONS = frozenset({"continue", "consult", "require_review", "block"})
_SIGNAL_NAMES = frozenset({
    "task_kind",
    "risk_flags",
    "scope_files",
    "ambiguity",
    "consequence",
    "attempts",
    "worker_failed",
    "verification_failed",
    "missing_evidence",
    "scope_changed",
    "explicit_authority",
    "revision_round",
})


class EscalationPolicyError(ValueError):
    """Raised when an escalation policy or decision input is malformed."""


def normalize_stage(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EscalationPolicyError("stage must be a non-empty string")
    stage = value.strip().lower().replace("-", "_")
    stage = STAGE_ALIASES.get(stage, stage)
    if stage not in STAGES:
        raise EscalationPolicyError(
            f"unknown escalation stage {value!r}; choose one of {', '.join(sorted(STAGES))}"
        )
    return stage


def _string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise EscalationPolicyError(f"{field} must be a string or list of non-empty strings")
    return tuple(item.strip() for item in value)


def _object(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise EscalationPolicyError(f"{field} must be an object")
    return dict(value)


@dataclass(frozen=True)
class EscalationProfile:
    name: str
    lane: str
    model: str
    reasoning_effort: str = "high"
    role: str = "verifier"

    @classmethod
    def from_dict(cls, name: str, value: Mapping[str, Any]) -> "EscalationProfile":
        raw = _object(value, f"profile {name}")
        if not isinstance(name, str) or not name.strip():
            raise EscalationPolicyError("profile names must be non-empty strings")
        lane = raw.get("lane")
        model = raw.get("model")
        if not isinstance(lane, str) or not lane.strip():
            raise EscalationPolicyError(f"profile {name!r} requires a lane")
        if not isinstance(model, str) or not model.strip():
            raise EscalationPolicyError(f"profile {name!r} requires a model")
        effort = raw.get("reasoning_effort", "high")
        role = raw.get("role", "verifier")
        if not isinstance(effort, str) or not effort.strip():
            raise EscalationPolicyError(f"profile {name!r} reasoning_effort must be non-empty")
        if not isinstance(role, str) or not role.strip():
            raise EscalationPolicyError(f"profile {name!r} role must be non-empty")
        return cls(name=name.strip(), lane=lane.strip(), model=model.strip(), reasoning_effort=effort.strip(), role=role.strip())

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "lane": self.lane,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "role": self.role,
        }


@dataclass(frozen=True)
class EscalationRule:
    rule_id: str
    priority: int
    stages: tuple[str, ...]
    action: str
    profile: str | None = None
    any_conditions: Mapping[str, Any] = None  # type: ignore[assignment]
    all_conditions: Mapping[str, Any] = None  # type: ignore[assignment]
    evidence_required: tuple[str, ...] = ()
    reason: str = ""
    max_revisions: int = 1
    on_reject: str = "revise"
    on_exhausted: str = "block"
    order: int = 0

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], order: int) -> "EscalationRule":
        raw = _object(value, "rule")
        rule_id = raw.get("id")
        if not isinstance(rule_id, str) or not rule_id.strip():
            raise EscalationPolicyError("each escalation rule requires a non-empty id")
        priority = raw.get("priority", 0)
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise EscalationPolicyError(f"rule {rule_id!r} priority must be an integer")
        stages_raw = raw.get("stages", ["any"])
        stages = _string_tuple(stages_raw, f"rule {rule_id!r} stages")
        normalized_stages = tuple(
            normalize_stage(stage) if stage != "any" else "any" for stage in stages
        )
        if not normalized_stages:
            raise EscalationPolicyError(f"rule {rule_id!r} must name at least one stage")
        action = raw.get("action")
        if action not in ACTIONS:
            raise EscalationPolicyError(
                f"rule {rule_id!r} action must be one of {', '.join(sorted(ACTIONS))}"
            )
        profile = raw.get("profile")
        if profile is not None and (not isinstance(profile, str) or not profile.strip()):
            raise EscalationPolicyError(f"rule {rule_id!r} profile must be a non-empty string")
        any_conditions = _object(raw.get("any", {}), f"rule {rule_id!r}.any")
        all_conditions = _object(raw.get("all", {}), f"rule {rule_id!r}.all")
        for condition_name in (*any_conditions, *all_conditions):
            if condition_name not in _SIGNAL_NAMES and not condition_name.endswith("_at_least"):
                raise EscalationPolicyError(
                    f"rule {rule_id!r} uses unsupported signal {condition_name!r}"
                )
        evidence = _string_tuple(raw.get("evidence_required", ()), f"rule {rule_id!r}.evidence_required")
        reason = raw.get("reason", "")
        if not isinstance(reason, str):
            raise EscalationPolicyError(f"rule {rule_id!r} reason must be a string")
        max_revisions = raw.get("max_revisions", 1 if action in {"consult", "require_review"} else 0)
        if not isinstance(max_revisions, int) or isinstance(max_revisions, bool) or max_revisions < 0:
            raise EscalationPolicyError(f"rule {rule_id!r} max_revisions must be a non-negative integer")
        on_reject = raw.get("on_reject", "revise")
        on_exhausted = raw.get("on_exhausted", "block")
        if on_reject not in {"revise", "block"}:
            raise EscalationPolicyError(f"rule {rule_id!r} on_reject must be revise or block")
        if on_exhausted not in {"block", "human_review"}:
            raise EscalationPolicyError(f"rule {rule_id!r} on_exhausted must be block or human_review")
        return cls(
            rule_id=rule_id.strip(),
            priority=priority,
            stages=normalized_stages,
            action=action,
            profile=profile.strip() if isinstance(profile, str) else None,
            any_conditions=any_conditions,
            all_conditions=all_conditions,
            evidence_required=evidence,
            reason=reason.strip(),
            max_revisions=max_revisions,
            on_reject=on_reject,
            on_exhausted=on_exhausted,
            order=order,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.rule_id,
            "priority": self.priority,
            "stages": list(self.stages),
            "any": dict(self.any_conditions or {}),
            "all": dict(self.all_conditions or {}),
            "action": self.action,
            "profile": self.profile,
            "evidence_required": list(self.evidence_required),
            "reason": self.reason,
            "max_revisions": self.max_revisions,
            "on_reject": self.on_reject,
            "on_exhausted": self.on_exhausted,
        }


def _condition_matches(name: str, expected: Any, signals: Mapping[str, Any]) -> bool:
    if name.endswith("_at_least"):
        base = name[: -len("_at_least")]
        actual = signals.get(base)
        return isinstance(actual, int) and not isinstance(actual, bool) and actual >= expected
    actual = signals.get(name)
    if isinstance(expected, list):
        if isinstance(actual, (list, tuple, set, frozenset)):
            return bool(set(actual).intersection(expected))
        return actual in expected
    return actual == expected


def _rule_matches(rule: EscalationRule, stage: str, signals: Mapping[str, Any]) -> bool:
    if "any" not in rule.stages and stage not in rule.stages:
        return False
    any_conditions = dict(rule.any_conditions or {})
    all_conditions = dict(rule.all_conditions or {})
    if any_conditions and not any(_condition_matches(name, value, signals) for name, value in any_conditions.items()):
        return False
    if all_conditions and not all(_condition_matches(name, value, signals) for name, value in all_conditions.items()):
        return False
    return True


@dataclass(frozen=True)
class EscalationDecision:
    action: str
    stage: str
    policy_version: int
    policy_sha256: str
    rule_id: str
    priority: int
    reasons: tuple[str, ...]
    signals: Mapping[str, Any]
    profile: EscalationProfile | None = None
    evidence_required: tuple[str, ...] = ()
    max_revisions: int = 0
    on_reject: str = "block"
    on_exhausted: str = "block"

    @property
    def escalated(self) -> bool:
        return self.action in {"consult", "require_review"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "escalated": self.escalated,
            "stage": self.stage,
            "policy_version": self.policy_version,
            "policy_sha256": self.policy_sha256,
            "rule_id": self.rule_id,
            "priority": self.priority,
            "reasons": list(self.reasons),
            "signals": dict(self.signals),
            "profile": self.profile.to_dict() if self.profile else None,
            "evidence_required": list(self.evidence_required),
            "feedback_loop": {
                "max_revisions": self.max_revisions,
                "on_reject": self.on_reject,
                "on_exhausted": self.on_exhausted,
            },
        }


DEFAULT_POLICY: dict[str, Any] = {
    "version": POLICY_VERSION,
    "enabled": True,
    "default_action": "continue",
    "profiles": {
        "high_planner": {
            "lane": "codex-escalation",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "role": "planner",
        },
        "high_verifier": {
            "lane": "sol-reviewer",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "role": "verifier",
        },
    },
    "rules": [
        {
            "id": "plan-end-second-opinion",
            "priority": 950,
            "stages": ["plan_end"],
            "action": "consult",
            "profile": "high_planner",
            "evidence_required": ["plan_feedback", "assumptions", "risks"],
            "max_revisions": 1,
            "on_reject": "revise",
            "on_exhausted": "human_review",
        },
        {
            "id": "review-end-second-opinion",
            "priority": 800,
            "stages": ["review_end"],
            "action": "require_review",
            "profile": "high_verifier",
            "evidence_required": ["independent_findings", "verification_receipt"],
            "max_revisions": 1,
            "on_reject": "revise",
            "on_exhausted": "human_review",
        },
        {
            "id": "recovery-after-failure",
            "priority": 750,
            "stages": ["recovery"],
            "any": {"worker_failed": True, "verification_failed": True, "attempts_at_least": 2},
            "action": "consult",
            "profile": "high_planner",
            "evidence_required": ["failure_history", "attempted_remedies", "recovery_plan"],
            "max_revisions": 1,
            "on_reject": "revise",
            "on_exhausted": "human_review",
        },
    ],
}


class EscalationPolicy:
    def __init__(self, raw: Mapping[str, Any]):
        value = _object(raw, "policy")
        version = value.get("version")
        if version != POLICY_VERSION:
            raise EscalationPolicyError(f"unsupported escalation policy version: {version!r}")
        enabled = value.get("enabled", True)
        if not isinstance(enabled, bool):
            raise EscalationPolicyError("policy enabled must be boolean")
        action = value.get("default_action", "continue")
        if action not in ACTIONS:
            raise EscalationPolicyError("default_action must be a supported escalation action")
        profiles_raw = _object(value.get("profiles", {}), "profiles")
        profiles = {
            name: EscalationProfile.from_dict(name, profile)
            for name, profile in profiles_raw.items()
        }
        rules_raw = value.get("rules", [])
        if not isinstance(rules_raw, list):
            raise EscalationPolicyError("rules must be a list")
        rules = tuple(EscalationRule.from_dict(item, index) for index, item in enumerate(rules_raw))
        if len({rule.rule_id for rule in rules}) != len(rules):
            raise EscalationPolicyError("rule ids must be unique")
        for rule in rules:
            if rule.profile and rule.profile not in profiles:
                raise EscalationPolicyError(f"rule {rule.rule_id!r} references unknown profile {rule.profile!r}")
            if rule.action in {"consult", "require_review"} and not rule.profile:
                    raise EscalationPolicyError(f"rule {rule.rule_id!r} requires a profile for {rule.action}")
        canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.version = version
        self.enabled = enabled
        self.default_action = action
        self.profiles = profiles
        self.rules = tuple(sorted(rules, key=lambda rule: (-rule.priority, rule.order)))
        self.sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def default(cls) -> "EscalationPolicy":
        return cls(DEFAULT_POLICY)

    @classmethod
    def from_path(cls, path: str | Path) -> "EscalationPolicy":
        candidate = Path(path).expanduser()
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except OSError as exc:
            raise EscalationPolicyError(f"could not read escalation policy {candidate}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise EscalationPolicyError(f"escalation policy is not valid JSON: {exc}") from exc
        return cls(value)

    def evaluate(self, stage: str, signals: Mapping[str, Any] | None = None) -> EscalationDecision:
        normalized_stage = normalize_stage(stage)
        observed = _object(signals or {}, "signals")
        for name in observed:
            if name not in _SIGNAL_NAMES:
                raise EscalationPolicyError(f"unsupported escalation signal {name!r}")
        observed["stage"] = normalized_stage
        if not self.enabled:
            return EscalationDecision(
                action="continue", stage=normalized_stage, policy_version=self.version,
                policy_sha256=self.sha256, rule_id="disabled", priority=0,
                reasons=("policy_disabled",), signals=observed,
                max_revisions=0, on_reject="block", on_exhausted="block",
            )
        for rule in self.rules:
            if _rule_matches(rule, normalized_stage, observed):
                profile = self.profiles.get(rule.profile) if rule.profile else None
                reasons = (rule.reason or f"matched rule {rule.rule_id}",)
                return EscalationDecision(
                    action=rule.action, stage=normalized_stage, policy_version=self.version,
                    policy_sha256=self.sha256, rule_id=rule.rule_id, priority=rule.priority,
                    reasons=reasons, signals=observed, profile=profile,
                    evidence_required=rule.evidence_required,
                    max_revisions=rule.max_revisions,
                    on_reject=rule.on_reject,
                    on_exhausted=rule.on_exhausted,
                )
        profile = None
        return EscalationDecision(
            action=self.default_action, stage=normalized_stage, policy_version=self.version,
            policy_sha256=self.sha256, rule_id="default", priority=-1,
            reasons=("no_rule_matched",), signals=observed, profile=profile,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "enabled": self.enabled,
            "default_action": self.default_action,
            "profiles": {name: profile.to_dict() for name, profile in self.profiles.items()},
            "rules": [rule.to_dict() for rule in self.rules],
        }


def load_policy(path: str | Path | None = None) -> EscalationPolicy:
    """Load an explicit policy or the conservative built-in default."""

    selected = path or os.environ.get("AR_ESCALATION_POLICY")
    if selected:
        return EscalationPolicy.from_path(selected)
    return EscalationPolicy.default()


__all__ = [
    "ACTIONS",
    "DEFAULT_POLICY",
    "EscalationDecision",
    "EscalationPolicy",
    "EscalationPolicyError",
    "EscalationProfile",
    "EscalationRule",
    "POLICY_VERSION",
    "STAGES",
    "load_policy",
    "normalize_stage",
]
