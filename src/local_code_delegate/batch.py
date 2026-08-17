from __future__ import annotations

import json
import hashlib
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

from .codex_worker import CodexCliConfig, CodexCliWorker
from .delegate import delegate_local
from .frontier import MANIFEST_MODES, frontier_budget, task_manifest, token_estimate
from .result import ResultStatus
from .task import DelegationTask
from .triage import TriageResult, triage_task


WorkerFactory = Callable[[Path, str | None], Any]


def _read_manifest(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, Mapping):
        entries = value.get("tasks")
    else:
        entries = value
    if not isinstance(entries, list) or not entries:
        raise ValueError("batch manifest must contain a non-empty tasks array")
    normalized: list[dict[str, Any]] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, Mapping):
            raise ValueError(f"batch task {index} must be an object")
        item = dict(entry)
        task_value = item.get("task")
        if isinstance(task_value, Mapping):
            task_data = dict(task_value)
        elif isinstance(item.get("task_file"), str):
            task_path = (path.parent / item["task_file"]).resolve()
            task_data = json.loads(task_path.read_text(encoding="utf-8"))
            if not isinstance(task_data, Mapping):
                raise ValueError(f"task_file {item['task_file']!r} must contain an object")
            task_data = dict(task_data)
        else:
            task_data = {
                key: value
                for key, value in item.items()
                if key not in {"repo", "expected_status", "label", "triage"}
            }
        normalized.append({
            "task": task_data,
            "repo": item.get("repo"),
            "expected_status": item.get("expected_status"),
            "label": item.get("label"),
            "triage": item.get("triage"),
        })
    return normalized


def _safe_task_name(task_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", task_id).strip("-.")
    return value or "task"


def _compact_triage(result: TriageResult) -> dict[str, Any]:
    value = result.to_dict()
    return {
        key: value[key]
        for key in (
            "decision",
            "confidence",
            "reason_codes",
            "risk_flags",
            "gates",
            "economics",
        )
    }


def _triage_entry(
    entry: Mapping[str, Any],
    task: DelegationTask,
    *,
    avoided_tokens: int | None,
    spent_tokens: int | None,
    minimum_leverage: float,
) -> TriageResult:
    """Apply one manifest entry's parent routing decision before a worker call."""

    raw = entry.get("triage")
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError(
            f"batch task {task.task_id!r} triage must be an object"
        )
    return triage_task(
        task,
        expected_codex_tokens_avoided=raw.get(
            "avoided_tokens", avoided_tokens
        ),
        expected_codex_tokens_spent=raw.get("spent_tokens", spent_tokens),
        minimum_leverage=raw.get("minimum_leverage", minimum_leverage),
    )


def _triage_handoff(
    task: DelegationTask,
    triage: TriageResult,
    patch_name: str,
) -> dict[str, Any]:
    """Create a compact proof packet for a task intentionally not delegated."""

    empty_patch_sha = hashlib.sha256(b"").hexdigest()
    handoff: dict[str, Any] = {
        "task_id": task.task_id,
        "status": triage.decision.value,
        "summary": triage.to_dict()["why"][:160],
        "files_changed": [],
        "verification": [],
        "patch": {
            "sha256": empty_patch_sha,
            "bytes": 0,
            "artifact": patch_name,
        },
        "triage": _compact_triage(triage),
        "not_delegated": True,
    }
    handoff["handoff_tokens_estimate"] = max(
        1,
        (len(json.dumps(handoff, ensure_ascii=False, separators=(",", ":"))) + 3)
        // 4,
    )
    return handoff


def run_batch(
    *,
    manifest: str | Path,
    repo: str | Path,
    model: str | None = None,
    artifact_dir: str | Path | None = None,
    aggregate: bool = False,
    sample: int = 0,
    manifest_mode: str = "full",
    worker_factory: WorkerFactory | None = None,
    require_triage: bool = False,
    avoided_tokens: int | None = None,
    spent_tokens: int | None = None,
    minimum_leverage: float = 2.0,
) -> dict[str, Any]:
    """Run independent bounded tasks and return one compact frontier handoff.

    Each task still receives its own disposable sandbox and verification pass.
    The batching is an orchestration boundary: the frontier agent receives one
    proof packet and opens individual patch artifacts only when needed.
    """

    if sample < 0:
        raise ValueError("sample must be nonnegative")
    if manifest_mode not in MANIFEST_MODES:
        raise ValueError(
            f"manifest_mode must be one of {', '.join(MANIFEST_MODES)}"
        )
    config = CodexCliConfig.from_env()
    selected_model = model or config.default_model
    root = Path(repo).resolve()
    manifest_path = Path(manifest).resolve()
    entries = _read_manifest(manifest_path)
    artifact_root = (
        Path(artifact_dir).resolve()
        if artifact_dir is not None
        else Path(tempfile.mkdtemp(prefix="lcd-batch-artifacts-"))
    )
    artifact_root.mkdir(parents=True, exist_ok=True)

    handoffs: list[dict[str, Any]] = []
    full_records: list[dict[str, Any]] = []
    seen_task_ids: set[str] = set()
    for entry in entries:
        task = DelegationTask.from_dict(entry["task"])
        if task.task_id in seen_task_ids:
            raise ValueError(f"duplicate batch task_id: {task.task_id}")
        seen_task_ids.add(task.task_id)
        task_repo = Path(entry["repo"]).resolve() if entry.get("repo") else root
        triage: TriageResult | None = None
        if require_triage:
            triage = _triage_entry(
                entry,
                task,
                avoided_tokens=avoided_tokens,
                spent_tokens=spent_tokens,
                minimum_leverage=minimum_leverage,
            )
            if not triage.can_delegate:
                patch_path = artifact_root / f"{_safe_task_name(task.task_id)}.patch"
                patch_path.write_text("", encoding="utf-8")
                handoff = _triage_handoff(task, triage, patch_path.name)
                if entry.get("expected_status") is not None:
                    handoff["expected_status"] = entry["expected_status"]
                if entry.get("label") is not None:
                    handoff["label"] = entry["label"]
                handoffs.append(handoff)
                full_records.append({
                    "task_id": task.task_id,
                    "expected_status": entry.get("expected_status"),
                    "label": entry.get("label"),
                    "result": None,
                    "handoff": handoff,
                    "triage": triage.to_dict(),
                })
                continue
        if worker_factory is not None:
            worker = worker_factory(task_repo, model)
        else:
            worker = CodexCliWorker(
                repo=task_repo,
                model=selected_model,
                config=config,
            )
        result = delegate_local(
            task=task,
            repo=task_repo,
            model=model,
            worker=worker,
        )
        patch_path = artifact_root / f"{_safe_task_name(task.task_id)}.patch"
        patch_path.write_text(result.patch, encoding="utf-8")
        handoff = result.to_handoff(patch_artifact=patch_path.name)
        handoff.pop("handoff_tokens_estimate", None)
        if triage is not None:
            handoff["triage"] = _compact_triage(triage)
        if entry.get("expected_status") is not None:
            handoff["expected_status"] = entry["expected_status"]
        if entry.get("label") is not None:
            handoff["label"] = entry["label"]
        handoffs.append(handoff)
        full_records.append({
            "task_id": task.task_id,
            "expected_status": entry.get("expected_status"),
            "label": entry.get("label"),
            "result": result.to_dict(),
            "handoff": handoff,
        })

    accepted_statuses = {ResultStatus.SUCCESS.value, ResultStatus.BLOCKED.value}
    def task_passed(item: Mapping[str, Any]) -> bool:
        expected = item.get("expected_status")
        if expected is not None:
            return item.get("status") == expected
        if item.get("not_delegated") is True:
            # A required triage rejection is only a pass when the manifest
            # explicitly expected that refusal. Never silently count a skipped
            # worker as a successful delegated task.
            return False
        return item.get("status") in accepted_statuses

    task_rows = [
        {
            "task_id": item.get("task_id"),
            "status": item.get("status"),
            "passed": task_passed(item),
        }
        for item in handoffs
    ]
    task_index = {
        "passed": [
            row["task_id"] for row in task_rows if row["passed"] is True
        ],
        "failed": [
            row["task_id"] for row in task_rows if row["passed"] is not True
        ],
    }
    task_status_counts: dict[str, int] = {}
    for row in task_rows:
        status = str(row["status"])
        task_status_counts[status] = task_status_counts.get(status, 0) + 1
    task_failures = [
        item for item, row in zip(handoffs, task_rows)
        if row["passed"] is not True
    ]
    passing_sample = [
        item for item, row in zip(handoffs, task_rows)
        if row["passed"] is True
    ][:sample]
    full_evidence_path = artifact_root / "full-records.json"
    full_evidence_path.write_text(
        json.dumps(
            {"manifest": str(manifest_path), "records": full_records},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    payload: dict[str, Any] = {
        "status": (
            "PASS"
            if all(row["passed"] is True for row in task_rows)
            else "FAIL"
        ),
        "backend": "codex-ollama",
        "model": selected_model,
        "review_mode": "aggregate-proof" if aggregate else "compact-handoff",
        "frontier_manifest_mode": manifest_mode,
        "task_count": len(handoffs),
        "artifact_dir": str(artifact_root),
        "full_evidence_artifact": full_evidence_path.name,
        "main_worktree_unchanged": all(
            item.get("main_worktree_unchanged", True) is True for item in handoffs
        ),
        "triage": {
            "required": require_triage,
            "delegated_tasks": sum(
                item.get("not_delegated") is not True for item in handoffs
            ),
            "not_delegated_tasks": sum(
                item.get("not_delegated") is True for item in handoffs
            ),
            "rejections": [
                {
                    "task_id": item.get("task_id"),
                    "status": item.get("status"),
                    "expected_status": item.get("expected_status"),
                }
                for item in handoffs
                if item.get("not_delegated") is True
            ],
        },
    }
    if aggregate:
        payload["tasks"] = []
        payload["task_index"] = task_index
        payload["task_status_counts"] = task_status_counts
        payload["task_failures"] = task_failures
        payload["task_review_sample"] = passing_sample
        payload["review_sample_count"] = len(passing_sample)
        payload["review_policy"] = (
            "deterministic verification plus failure review and deterministic "
            "passing sample review"
            if sample > 0
            else "failure review only; passing tasks require external "
            "full-evidence review or a nonzero sample"
        )
        review_artifacts = [
            item["patch"]["artifact"]
            for item in (*task_failures, *passing_sample)
            if isinstance(item.get("patch"), Mapping)
            and isinstance(item["patch"].get("artifact"), str)
        ]
    else:
        payload["tasks"] = handoffs
        review_artifacts = []

    full_payload: dict[str, Any] = {
        "backend": "codex-ollama",
        "model": selected_model,
        "task_count": len(handoffs),
        "tasks": full_records,
    }
    payload["frontier_handoff_chars"] = 0
    payload["frontier_handoff_tokens_estimate"] = 0
    frontier_budget(
        full_payload=full_payload,
        compact_payload=payload,
        artifact_root=artifact_root,
        review_artifacts=review_artifacts,
        task_manifest=task_manifest(
            [{"id": item["task"].get("task_id"), "task": item["task"]} for item in entries],
            manifest_mode,
        ),
        task_manifest_mode=manifest_mode,
    )
    for _ in range(3):
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        payload["frontier_handoff_chars"] = len(serialized)
        payload["frontier_handoff_tokens_estimate"] = token_estimate(serialized)
        budget = payload["frontier_budget"]
        if isinstance(budget, Mapping):
            budget["compact_handoff_tokens_estimate"] = payload[
                "frontier_handoff_tokens_estimate"
            ]
            full_tokens = int(budget.get("full_report_tokens_estimate", 0))
            budget["response_compaction_reduction_estimate"] = (
                (full_tokens - payload["frontier_handoff_tokens_estimate"])
                / full_tokens
                if full_tokens
                else None
            )
            review_tokens = int(budget.get("review_artifact_tokens_estimate", 0))
            manifest_tokens = int(budget.get("task_manifest_tokens_estimate") or 0)
            budget["frontier_tokens_with_selected_review_estimate"] = (
                payload["frontier_handoff_tokens_estimate"] + review_tokens
            )
            budget["selected_review_reduction_estimate"] = (
                (
                    full_tokens
                    - payload["frontier_handoff_tokens_estimate"]
                    - review_tokens
                )
                / full_tokens
                if full_tokens
                else None
            )
            budget["frontier_tokens_with_manifest_and_review_estimate"] = (
                payload["frontier_handoff_tokens_estimate"]
                + review_tokens
                + manifest_tokens
            )
    return payload
