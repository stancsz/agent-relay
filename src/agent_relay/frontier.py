from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence


MANIFEST_MODES = ("full", "contract", "thin", "none")


def task_manifest(
    cases: Sequence[Mapping[str, Any]],
    mode: str = "full",
) -> list[dict[str, Any]] | None:
    """Build the task-definition payload that the frontier must provide.

    ``full`` is conservative. ``contract`` omits read-only context because the
    local harness gathers it. ``thin`` keeps only the routing and verification
    fields. ``none`` models a predeclared suite where the frontier already has
    the task contract and sends only the suite/batch command.
    """

    if mode not in MANIFEST_MODES:
        raise ValueError(
            f"manifest mode must be one of {', '.join(MANIFEST_MODES)}"
        )
    if mode == "none":
        return None
    contract_fields = (
        "task_id",
        "objective",
        "allowed_files",
        "requirements",
        "constraints",
        "verification",
        "success_criteria",
        "model",
        "retry_limit",
        "context_mode",
        "task_kind",
        "risk_flags",
    )
    thin_fields = (
        "task_id",
        "objective",
        "allowed_files",
        "verification",
        "task_kind",
        "risk_flags",
    )
    fields = contract_fields if mode == "contract" else thin_fields
    result: list[dict[str, Any]] = []
    for case in cases:
        task = case.get("task")
        if not isinstance(task, Mapping):
            raise ValueError("every case must contain a task object")
        result.append({
            "id": case.get("id"),
            "task": dict(task)
            if mode == "full"
            else {
                key: task[key]
                for key in fields
                if key in task and task[key] is not None
            },
        })
    return result


def token_estimate(value: Any) -> int:
    """Return a transparent, deterministic frontier-token estimate.

    This is intentionally a response-size estimate, not provider telemetry.
    Keeping the rule in one place makes compact-versus-full comparisons
    reproducible and prevents local-model usage from being mistaken for
    frontier Codex usage.
    """

    if isinstance(value, str):
        serialized = value
    else:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    return max(1, (len(serialized) + 3) // 4)


def artifact_token_estimate(
    artifact_root: Path,
    artifact_names: Sequence[str],
) -> int:
    """Estimate tokens for artifacts that the frontier review policy opens."""

    total = 0
    for name in artifact_names:
        path = artifact_root / name
        if path.is_file():
            total += token_estimate(path.read_text(encoding="utf-8"))
    return total


def frontier_budget(
    *,
    full_payload: Mapping[str, Any],
    compact_payload: dict[str, Any],
    artifact_root: Path,
    review_artifacts: Sequence[str] = (),
    task_manifest: Any | None = None,
    task_manifest_mode: str | None = None,
) -> tuple[dict[str, Any], int]:
    """Attach a measured compact-handoff budget to a report.

    ``full_payload`` is the same run serialized with full per-task records.
    ``compact_payload`` is the report that will cross the frontier boundary.
    The returned budget includes the compact packet and only the artifacts
    named by the declared review policy. It deliberately does not invent
    task-decomposition, repair, or recovery costs.
    """

    full_tokens = token_estimate(full_payload)
    review_names = list(dict.fromkeys(str(name) for name in review_artifacts))
    review_tokens = artifact_token_estimate(artifact_root, review_names)
    manifest_tokens = (
        token_estimate(task_manifest) if task_manifest is not None else None
    )
    budget: dict[str, Any] = {
        "method": "utf8_characters_div_4",
        "full_report_tokens_estimate": full_tokens,
        "review_artifact_tokens_estimate": review_tokens,
        "review_artifacts": review_names,
        "task_manifest_tokens_estimate": manifest_tokens,
        "task_manifest_mode": task_manifest_mode,
        "unpriced_costs": [
            "parent triage/decision record",
            "frontier task selection/decomposition",
            "Codex repair/recovery after artifact review",
        ],
    }

    # The budget is part of the packet itself. Recompute after adding it so
    # the reported handoff size includes the accounting metadata.
    compact_payload["frontier_budget"] = budget
    for _ in range(3):
        compact_tokens = token_estimate(compact_payload)
        budget["compact_handoff_tokens_estimate"] = compact_tokens
        budget["frontier_tokens_with_selected_review_estimate"] = (
            compact_tokens + review_tokens
        )
        budget["response_compaction_reduction_estimate"] = (
            (full_tokens - compact_tokens) / full_tokens
            if full_tokens
            else None
        )
        budget["selected_review_reduction_estimate"] = (
            (full_tokens - compact_tokens - review_tokens) / full_tokens
            if full_tokens
            else None
        )
        budget["frontier_tokens_with_manifest_and_review_estimate"] = (
            compact_tokens + review_tokens + (manifest_tokens or 0)
        )

    final_tokens = token_estimate(compact_payload)
    return budget, final_tokens
