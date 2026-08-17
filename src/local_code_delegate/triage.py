"""Conservative parent-side delegation eligibility checks.

The worker harness proves whether a delegated patch is safe and correct.  This
module answers the earlier question: should the frontier agent delegate this
task at all?  It is intentionally deterministic and explainable so a prompt
or an automation can use it without asking another model to make the routing
decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any, Mapping

from .task import DelegationTask


class DelegationDecision(str, Enum):
    DELEGATE = "DELEGATE"
    KEEP_LOCAL = "KEEP_LOCAL"
    BLOCKED = "BLOCKED"


class TriageConfidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


SAFE_TASK_KINDS = frozenset({
    "mechanical",
    "test_generation",
    "repetitive",
    "bounded_bugfix",
    "documentation",
})

_IGNORED_RISK_FLAGS = frozenset({"none", "low_risk", "low"})

# These are intentionally conservative routing signals.  They do not attempt
# to understand the implementation; they keep work requiring frontier
# judgment in the parent context.
_RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("architecture", re.compile(r"\b(?:architect(?:ure|ural)?|redesign|cross[ -]?cutting)\b")),
    ("security", re.compile(r"\b(?:security|authentication|authorization|access control|oauth)\b")),
    ("credentials", re.compile(r"\b(?:credentials?|passwords?|secrets?|private key|api key|certificates?)\b")),
    ("migration", re.compile(r"\b(?:database|schema)\s+migration\b|\bmigrate\b")),
    ("production", re.compile(r"\b(?:production|deploy|deployment|release|infrastructure)\b")),
    ("destructive", re.compile(r"\b(?:delete|destroy|drop table|irreversible)\b")),
    ("broad_change", re.compile(r"\b(?:refactor|rewrite)\b|\b(?:entire|whole)\s+(?:repo|repository|codebase|project)\b")),
    ("performance", re.compile(r"\b(?:performance|latency|throughput|benchmark)\b")),
    ("ambiguous", re.compile(r"\b(?:investigate|explore|figure out|find out|diagnose|decide|determine)\b")),
)


@dataclass(frozen=True)
class TriageResult:
    decision: DelegationDecision
    confidence: TriageConfidence
    reason_codes: tuple[str, ...]
    risk_flags: tuple[str, ...]
    gates: Mapping[str, bool]
    expected_codex_tokens_avoided: int | None = None
    expected_codex_tokens_spent: int | None = None
    minimum_leverage: float = 2.0
    leverage: float | None = None
    net_savings_rate: float | None = None
    max_allowed_files: int = 3
    max_context_items: int = 6

    @property
    def can_delegate(self) -> bool:
        return self.decision is DelegationDecision.DELEGATE

    def to_dict(self) -> dict[str, Any]:
        if self.can_delegate:
            why = (
                "All safety gates passed and the expected frontier-token "
                f"leverage is {self.leverage:.2f}x."
                if self.leverage is not None
                else "All safety gates passed."
            )
            next_action = "Build the minimal contract and call lcd delegate or lcd batch."
        elif self.decision is DelegationDecision.BLOCKED:
            why = "The task contract is incomplete for a safe delegation decision."
            next_action = "Complete the required contract fields, then run triage again."
        else:
            why = "Keep this work in the parent Codex context or split it into a smaller task."
            next_action = "Do not call the local worker for this task as currently specified."
        return {
            "decision": self.decision.value,
            "confidence": self.confidence.value,
            "why": why,
            "next_action": next_action,
            "reason_codes": list(self.reason_codes),
            "risk_flags": list(self.risk_flags),
            "gates": dict(self.gates),
            "economics": {
                "expected_codex_tokens_avoided": self.expected_codex_tokens_avoided,
                "expected_codex_tokens_spent": self.expected_codex_tokens_spent,
                "minimum_leverage": self.minimum_leverage,
                "leverage": self.leverage,
                "net_savings_rate": self.net_savings_rate,
                "method": "parent estimate; not provider telemetry",
            },
            "limits": {
                "max_allowed_files": self.max_allowed_files,
                "max_context_items": self.max_context_items,
            },
        }


def _normalise_risk_flags(values: tuple[str, ...]) -> list[str]:
    return list(dict.fromkeys(
        value for value in values if value not in _IGNORED_RISK_FLAGS
    ))


def _inferred_risk_flags(task: DelegationTask) -> list[str]:
    text = "\n".join((
        task.objective,
        *task.requirements,
        *task.constraints,
        *task.success_criteria,
    )).casefold()
    return [name for name, pattern in _RISK_PATTERNS if pattern.search(text)]


def _valid_token_value(value: int | None) -> bool:
    return (
        value is None
        or (isinstance(value, int) and not isinstance(value, bool) and value > 0)
    )


def triage_task(
    task: DelegationTask,
    *,
    expected_codex_tokens_avoided: int | None = None,
    expected_codex_tokens_spent: int | None = None,
    minimum_leverage: float = 2.0,
    max_allowed_files: int = 3,
    max_context_items: int = 6,
) -> TriageResult:
    """Return a deterministic delegate/keep-local recommendation.

    ``expected_codex_tokens_spent`` must include the triage decision record,
    parent decomposition, compact handoff, expected review, and a bounded
    repair/recovery allowance.  Missing
    economics is a conservative ``KEEP_LOCAL`` result rather than an assumed
    saving.  This makes the command useful as a real routing gate while keeping
    the estimate visibly separate from provider telemetry.
    """

    if not isinstance(max_allowed_files, int) or max_allowed_files < 1:
        raise ValueError("max_allowed_files must be a positive integer")
    if not isinstance(max_context_items, int) or max_context_items < 0:
        raise ValueError("max_context_items must be a nonnegative integer")
    if not isinstance(minimum_leverage, (int, float)) or isinstance(minimum_leverage, bool):
        raise ValueError("minimum_leverage must be a number")
    if minimum_leverage < 1.0:
        raise ValueError("minimum_leverage must be at least 1.0")

    explicit_flags = _normalise_risk_flags(task.risk_flags)
    inferred_flags = _inferred_risk_flags(task)
    risk_flags = list(dict.fromkeys((*explicit_flags, *inferred_flags)))
    reasons: list[str] = []
    blocked_reasons: list[str] = []

    kind_declared = task.task_kind != "unspecified"
    safe_kind = task.task_kind in SAFE_TASK_KINDS
    if not kind_declared:
        blocked_reasons.append("task_kind_required")
    elif not safe_kind:
        reasons.append("task_kind_keep_local")

    file_count = len(task.allowed_files)
    scope_bounded = 1 <= file_count <= max_allowed_files
    if file_count == 0:
        blocked_reasons.append("allowed_files_required")
    elif file_count > max_allowed_files:
        reasons.append("write_scope_too_broad")

    verification_commands = tuple(command.strip() for command in task.verification)
    verification_bounded = bool(verification_commands) and all(
        command.casefold() not in {"manual", "manual review", "review only", "todo"}
        and not command.casefold().startswith("todo:")
        for command in verification_commands
    )
    if not verification_bounded:
        blocked_reasons.append("deterministic_verification_required")

    context_bounded = len(task.context) <= max_context_items
    if not context_bounded:
        reasons.append("context_too_broad")

    if risk_flags:
        reasons.append("risk_flags_present")

    economics_valid = _valid_token_value(
        expected_codex_tokens_avoided
    ) and _valid_token_value(expected_codex_tokens_spent)
    leverage: float | None = None
    net_savings_rate: float | None = None
    economics_positive = False
    if not economics_valid:
        reasons.append("economics_invalid")
    elif expected_codex_tokens_avoided is None or expected_codex_tokens_spent is None:
        reasons.append("economics_unpriced")
    else:
        leverage = expected_codex_tokens_avoided / expected_codex_tokens_spent
        net_savings_rate = (
            expected_codex_tokens_avoided - expected_codex_tokens_spent
        ) / expected_codex_tokens_avoided
        economics_positive = leverage >= minimum_leverage
        if not economics_positive:
            reasons.append("economics_margin_too_small")

    gates = {
        "task_kind_declared": kind_declared,
        "safe_task_kind": safe_kind,
        "write_scope_bounded": scope_bounded,
        "deterministic_verification": verification_bounded,
        "context_bounded": context_bounded,
        "no_risk_flags": not risk_flags,
        "economics_positive": economics_positive,
    }

    if blocked_reasons:
        decision = DelegationDecision.BLOCKED
        reason_codes = tuple(dict.fromkeys((*blocked_reasons, *reasons)))
        confidence = TriageConfidence.HIGH
    elif reasons:
        decision = DelegationDecision.KEEP_LOCAL
        reason_codes = tuple(dict.fromkeys(reasons))
        confidence = TriageConfidence.HIGH
    else:
        decision = DelegationDecision.DELEGATE
        reason_codes = ()
        confidence = TriageConfidence.HIGH

    return TriageResult(
        decision=decision,
        confidence=confidence,
        reason_codes=reason_codes,
        risk_flags=tuple(risk_flags),
        gates=gates,
        expected_codex_tokens_avoided=expected_codex_tokens_avoided,
        expected_codex_tokens_spent=expected_codex_tokens_spent,
        minimum_leverage=float(minimum_leverage),
        leverage=leverage,
        net_savings_rate=net_savings_rate,
        max_allowed_files=max_allowed_files,
        max_context_items=max_context_items,
    )
