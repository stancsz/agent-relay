from __future__ import annotations

import json
import hashlib
import math
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Any, Mapping, Sequence

from agent_relay.delegate import delegate_local
from agent_relay.codex_worker import CodexCliConfig, CodexCliWorker
from agent_relay.frontier import (
    MANIFEST_MODES,
    frontier_budget,
    task_manifest,
    token_estimate,
)
from agent_relay.ollama import OllamaClient, OllamaConfig
from agent_relay.patch import PatchError, check_patch, patch_paths
from agent_relay.result import ResultStatus, WorkerResponse
from agent_relay.task import DelegationTask, context_path_and_range
from evals.scope_review import review_task_patch


class FixtureWorker:
    def __init__(self, case: Mapping[str, Any], cases_root: Path) -> None:
        self.case = case
        self.cases_root = cases_root

    def run(self, task: DelegationTask, context: str, retry: Any = None) -> WorkerResponse:
        expected = self.case["expected_status"]
        if expected == "BLOCKED":
            return WorkerResponse(
                status="BLOCKED",
                summary="Fixture correctly identifies an underspecified task.",
                blockers=("requirements are intentionally underspecified",),
            )
        patch_file = self.case.get("patch_file")
        if not patch_file:
            raise ValueError("fixture case has no patch_file")
        patch = (self.cases_root / patch_file).read_text(encoding="utf-8")
        return WorkerResponse(
            status="READY",
            summary="Fixture worker produced the expected bounded patch.",
            patch=patch,
        )


def _load_suite(
    suite: str,
    cases_root: Path,
    _seen: tuple[str, ...] = (),
) -> Mapping[str, Any]:
    path = cases_root / f"{suite}.json"
    if not path.is_file():
        raise FileNotFoundError(f"unknown eval suite: {suite}")
    if suite in _seen:
        raise ValueError(f"cyclic eval suite include: {' -> '.join((*_seen, suite))}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"eval suite must contain a JSON object: {suite}")
    cases: list[Mapping[str, Any]] = []
    includes = value.get("include", [])
    if isinstance(includes, str):
        includes = [includes]
    if not isinstance(includes, list) or any(not isinstance(item, str) for item in includes):
        raise ValueError(f"eval suite include must be a list of suite names: {suite}")
    for included in includes:
        included_value = _load_suite(included, cases_root, (*_seen, suite))
        cases.extend(included_value.get("cases", []))
    declared_cases = value.get("cases", [])
    if not isinstance(declared_cases, list):
        raise ValueError(f"eval suite cases must be a list: {suite}")
    cases.extend(declared_cases)
    return {**value, "cases": cases}


def _patch_hunk_old_lines(patch: str) -> list[str]:
    """Extract the old-side lines from the first canonical patch hunk."""

    lines = patch.splitlines()
    try:
        start = next(
            index for index, line in enumerate(lines) if line.startswith("@@ ")
        )
    except StopIteration as exc:
        raise ValueError("expected patch has no unified hunk") from exc
    old_lines: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("@@ "):
            break
        if line.startswith((" ", "-")):
            old_lines.append(line[1:])
        elif line.startswith(("+", "\\")):
            continue
        elif line:
            raise ValueError(f"expected patch has malformed hunk line: {line!r}")
    if not old_lines:
        raise ValueError("expected patch hunk has no old-side context")
    return old_lines


def _validate_declared_fixture_patches(
    cases: Sequence[Mapping[str, Any]],
    *,
    cases_root: Path,
    fixtures_root: Path,
) -> None:
    """Fail before model execution when a checked-in oracle is not trustworthy.

    A patch that merely applies is not enough for an ``insert_after`` case: its
    hunk must be anchored to the task's declared read-only context. Without
    this preflight, a stale oracle can make the fixture backend pass while
    silently testing a different insertion location than the model receives.
    """

    for case in cases:
        if case.get("eligibility") != "eligible":
            continue
        patch_name = case.get("patch_file")
        if not isinstance(patch_name, str) or not patch_name.strip():
            raise ValueError(f"eligible case {case.get('id')!r} has no patch_file")
        patch_path = (cases_root / patch_name).resolve()
        if not patch_path.is_file():
            raise ValueError(
                f"eligible case {case.get('id')!r} patch_file is missing: {patch_name}"
            )
        patch = patch_path.read_text(encoding="utf-8")
        fixture = (fixtures_root / str(case.get("fixture", ""))).resolve()
        if not fixture.is_dir():
            raise ValueError(
                f"eligible case {case.get('id')!r} fixture is missing: {fixture}"
            )
        try:
            paths = patch_paths(patch)
            check_patch(fixture, patch)
        except (PatchError, OSError, ValueError) as exc:
            raise ValueError(
                f"eligible case {case.get('id')!r} has an unappliable oracle: {exc}"
            ) from exc
        expected_files = tuple(
            sorted(str(path) for path in case.get("expected_files", []))
        )
        if tuple(sorted(paths)) != expected_files:
            raise ValueError(
                f"eligible case {case.get('id')!r} oracle paths {sorted(paths)} "
                f"do not match expected_files {list(expected_files)}"
            )
        task_data = case.get("task", {})
        if not isinstance(task_data, Mapping) or task_data.get("context_mode") != "insert_after":
            continue
        contexts = task_data.get("context", [])
        if not isinstance(contexts, list) or len(contexts) != 1:
            raise ValueError(
                f"eligible insert_after case {case.get('id')!r} must have one context spec"
            )
        context_path, start, end = context_path_and_range(str(contexts[0]))
        if start is None or end is None or len(paths) != 1 or paths[0] != context_path:
            raise ValueError(
                f"eligible insert_after case {case.get('id')!r} has an invalid context anchor"
            )
        source_path = fixture / Path(*context_path.split("/"))
        source_lines = source_path.read_text(encoding="utf-8").splitlines()
        declared_context = source_lines[start - 1 : end]
        actual_context = _patch_hunk_old_lines(patch)
        if actual_context != declared_context:
            raise ValueError(
                f"eligible insert_after case {case.get('id')!r} oracle hunk does not "
                f"match {contexts[0]}"
            )


def _cohort_identity(
    *,
    backend: str,
    suite: str,
    fixture_digest: str,
    repository_identity: str,
    selected_model: str | None,
    cases: Sequence[Mapping[str, Any]],
    codex_config: CodexCliConfig | None,
    runtime_config: OllamaConfig | None,
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the exact identity that a matched economics ledger must carry.

    Coarse fields such as suite/model/backend are not enough: two runs can use
    the same names while differing in task selection, provider wire mode, or
    compatibility-prompt settings.  Keep the descriptor deterministic and hash
    it so a hand-authored economics file cannot silently attach to another run.
    """

    eligible_case_ids = [
        str(case["id"])
        for case in cases
        if case["eligibility"] == "eligible"
    ]
    blocked_case_ids = [
        str(case["id"])
        for case in cases
        if case["eligibility"] == "blocked_expected"
    ]
    invalid_case_ids = [
        str(case["id"])
        for case in cases
        if case["eligibility"] == "invalid_fixture"
    ]
    observed_codex_versions = sorted({
        str(runtime["codex_version"])
        for record in records
        for runtime in [record.get("local_runtime", {})]
        if isinstance(runtime, Mapping)
        and isinstance(runtime.get("codex_version"), str)
        and runtime["codex_version"].strip()
    })
    if codex_config is not None:
        runtime_identity: dict[str, Any] = {
            "host": codex_config.ollama_host,
            "provider_id": codex_config.provider_id,
            "wire_api": codex_config.wire_api,
            "compat_proxy_enabled": codex_config.compat_proxy_enabled,
            "disable_reasoning": codex_config.disable_reasoning,
            "strip_tools": codex_config.strip_tools,
            "compact_prompt": codex_config.compact_prompt,
            "num_ctx": codex_config.ollama_num_ctx,
            "num_predict": codex_config.ollama_num_predict,
            "temperature": codex_config.ollama_temperature,
            "seed": codex_config.ollama_seed,
            "codex_versions": observed_codex_versions,
        }
    elif runtime_config is not None:
        runtime_identity = {
            "host": runtime_config.host,
            "temperature": runtime_config.temperature,
            "num_predict": runtime_config.num_predict,
            "think": runtime_config.think,
            "seed": runtime_config.seed,
        }
    else:
        runtime_identity = {}
    descriptor: dict[str, Any] = {
        "suite": suite,
        "fixture_digest": fixture_digest,
        "repository_identity": repository_identity,
        "model": selected_model or "<none>",
        "backend": backend,
        "case_ids": [str(case["id"]) for case in cases],
        "eligible_case_ids": eligible_case_ids,
        "blocked_expected_case_ids": blocked_case_ids,
        "invalid_fixture_case_ids": invalid_case_ids,
        "runtime": runtime_identity,
    }
    canonical = json.dumps(
        descriptor,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    descriptor["cohort_key"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return descriptor


def _case_passed(case: Mapping[str, Any], result: Any) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    expected_status = ResultStatus(case["expected_status"])
    if result.status is not expected_status:
        reasons.append(f"expected {expected_status.value}, got {result.status.value}")
    expected_files = sorted(case.get("expected_files", []))
    actual_files = sorted(result.files_changed)
    if expected_files != actual_files:
        reasons.append(f"expected files {expected_files}, got {actual_files}")
    if result.status is ResultStatus.SUCCESS and not all(item.passed for item in result.verification):
        reasons.append("one or more verification commands failed")
    for fragment in case.get("expected_patch_fragments", []):
        if fragment not in result.patch:
            reasons.append(f"expected patch fragment was not found: {fragment!r}")
    return not reasons, reasons


def _aggregate(cases: list[Mapping[str, Any]], records: list[Mapping[str, Any]]) -> dict[str, Any]:
    eligible = [case for case in cases if case["eligibility"] == "eligible"]
    eligible_records = [
        record for record in records if record["eligibility"] == "eligible"
    ]
    accepted = sum(record["passed"] for record in eligible_records)
    first_attempt = sum(
        record["passed"] and record["attempts"] == 1
        for record in eligible_records
    )
    verified = sum(
        record["passed"] and record["verification_passed"]
        for record in eligible_records
    )
    repair_values = [record["substantial_codex_repair"] for record in eligible_records]
    repairs = sum(value is True for value in repair_values)
    repair_observed = all(value is not None for value in repair_values)
    blocked = [record for record in records if record["eligibility"] == "blocked_expected"]
    blocked_correct = sum(record["status"] == "BLOCKED" for record in blocked)
    inner_sandbox_tasks = sum(
        "inner_sandbox_diff" in (record.get("local_runtime", {}).get("result_sources", []))
        for record in eligible_records
    )
    reported_candidate_tasks = sum(
        any(
            source in {"reported_patch", "reported_files"}
            for source in record.get("local_runtime", {}).get("result_sources", [])
        )
        for record in eligible_records
    )
    # Refusals are not delegated tasks. Scope is therefore measured over the
    # eligible delegation cohort, and remains unknown until every delegated
    # record has an explicit manual/automated scope review attestation.
    scope = sum(record["scope_violation"] for record in eligible_records)
    invalid = [record for record in records if record["eligibility"] == "invalid_fixture"]
    return {
        "eligible_tasks": len(eligible),
        "first_attempt_acceptances": first_attempt,
        "bounded_acceptances": accepted,
        "verification_passes": verified,
        "scope_violations": scope,
        "substantial_codex_repairs": repairs,
        "first_attempt_acceptance_rate": first_attempt / len(eligible) if eligible else None,
        "bounded_acceptance_rate": accepted / len(eligible) if eligible else None,
        "verification_pass_rate": verified / len(eligible) if eligible else None,
        "scope_violation_rate": (
            scope / len(eligible_records)
            if eligible_records and all(
                record.get("scope_reviewed") is True for record in eligible_records
            )
            else None
        ),
        "substantial_codex_repair_rate": (
            repairs / len(eligible) if eligible and repair_observed else None
        ),
        "scope_reviewed_tasks": sum(
            record.get("scope_reviewed") is True for record in eligible_records
        ),
        "scope_review_complete": all(
            record.get("scope_reviewed") is True for record in eligible_records
        ),
        "blocked_expected_tasks": len(blocked),
        "blocked_task_correctness": blocked_correct / len(blocked) if blocked else None,
        "inner_sandbox_diff_tasks": inner_sandbox_tasks,
        "reported_candidate_tasks": reported_candidate_tasks,
        "codex_tool_execution_share": (
            inner_sandbox_tasks / len(eligible_records)
            if eligible_records
            else None
        ),
        "invalid_fixture_tasks": len(invalid),
        "codex_token_reduction": "not measured by fixture harness",
        "wall_clock_overhead": "not measured by fixture harness",
    }


def _economics_report(
    eligible_records: list[Mapping[str, Any]],
    economics: Mapping[str, Any] | None,
    expected_cohort: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if economics is None:
        return {
            "status": "NOT_AVAILABLE",
            "reason": "Provide --economics with matched Codex-only baseline and review records.",
        }
    source = economics.get("source")
    if source not in {"codex-telemetry", "estimate"}:
        return {
            "status": "INVALID",
            "reason": "economics.source must be codex-telemetry or estimate",
        }
    cohort = economics.get("cohort")
    if not isinstance(cohort, Mapping):
        return {
            "status": "INVALID",
            "reason": "economics.cohort must identify the matched benchmark",
        }
    # A token ledger is only comparable when it identifies the execution
    # backend as well as the suite, fixture, repository, and model. In
    # particular, direct Ollama and Codex-as-harness runs are different
    # cohorts even when they use the same Qwen tag.
    cohort_required = (
        "suite",
        "fixture_digest",
        "repository_identity",
        "model",
        "backend",
    )
    if expected_cohort is not None or source == "codex-telemetry":
        cohort_required = (*cohort_required, "cohort_key")
    missing_cohort = [field for field in cohort_required if not isinstance(cohort.get(field), str) or not cohort[field].strip()]
    if missing_cohort:
        return {
            "status": "INCOMPLETE",
            "reason": "economics.cohort is missing required identity fields",
            "missing": missing_cohort,
        }
    cohort_warnings: list[str] = []
    if expected_cohort is not None:
        match_fields = cohort_required
        mismatches = {
            field: {"expected": expected_cohort.get(field), "actual": cohort.get(field)}
            for field in match_fields
            if str(expected_cohort.get(field)) != str(cohort.get(field))
        }
        if mismatches:
            return {
                "status": "INVALID",
                "reason": "economics cohort does not match the executed benchmark",
                "mismatches": mismatches,
            }
    if source == "codex-telemetry":
        provenance = economics.get("provenance")
        if not isinstance(provenance, Mapping):
            return {
                "status": "INCOMPLETE",
                "reason": (
                    "codex-telemetry economics requires a provenance object "
                    "with the capture run and usage artifact"
                ),
                "missing": ["provenance"],
            }
        missing_provenance = [
            field
            for field in ("run_id", "usage_artifact", "captured_at_utc")
            if not isinstance(provenance.get(field), str)
            or not provenance[field].strip()
        ]
        if missing_provenance:
            return {
                "status": "INCOMPLETE",
                "reason": "codex-telemetry provenance is incomplete",
                "missing": [f"provenance.{field}" for field in missing_provenance],
            }
    task_values = economics.get("tasks", economics)
    if not isinstance(task_values, Mapping):
        return {"status": "INVALID", "reason": "economics.tasks must be an object"}
    expected_task_ids = {str(record["id"]) for record in eligible_records}
    actual_task_ids = {str(key) for key in task_values}
    missing_task_ids = sorted(expected_task_ids - actual_task_ids)
    extra_task_ids = sorted(actual_task_ids - expected_task_ids)
    if missing_task_ids or extra_task_ids:
        return {
            "status": "INVALID",
            "reason": "economics task set does not match executed eligible cases",
            "missing_tasks": missing_task_ids,
            "extra_tasks": extra_task_ids,
        }
    required = (
        "baseline_codex_tokens",
        "delegation_codex_tokens",
        "review_codex_tokens",
        "repair_codex_tokens",
        "recovery_codex_tokens",
        "baseline_seconds",
        "delegation_seconds",
        "review_seconds",
        "repair_seconds",
        "recovery_seconds",
        "scope_reviewed",
        "substantial_codex_repair",
    )
    optional_numeric = ("triage_codex_tokens", "triage_seconds")
    triage_unpriced: list[str] = []
    missing: list[str] = []
    values: list[Mapping[str, Any]] = []
    for record in eligible_records:
        value = task_values.get(record["id"])
        if not isinstance(value, Mapping):
            missing.append(record["id"])
            continue
        missing_fields = [field for field in required if field not in value]
        if missing_fields:
            missing.append(f"{record['id']}: {', '.join(missing_fields)}")
            continue
        invalid_fields = []
        for field in required:
            value_field = value[field]
            if field in {"scope_reviewed", "substantial_codex_repair"}:
                if not isinstance(value_field, bool):
                    invalid_fields.append(field)
                continue
            if (
                isinstance(value_field, bool)
                or not isinstance(value_field, (int, float))
                or not math.isfinite(float(value_field))
                or float(value_field) < 0
            ):
                invalid_fields.append(field)
        for field in optional_numeric:
            if field not in value:
                triage_unpriced.append(f"{record['id']}: {field}")
                continue
            value_field = value[field]
            if (
                isinstance(value_field, bool)
                or not isinstance(value_field, (int, float))
                or not math.isfinite(float(value_field))
                or float(value_field) < 0
            ):
                invalid_fields.append(field)
        if invalid_fields:
            return {
                "status": "INVALID",
                "reason": f"nonnegative finite numeric/boolean validation failed for {record['id']}",
                "fields": invalid_fields,
            }
        if source == "codex-telemetry":
            usage = value.get("codex_usage")
            if not isinstance(usage, Mapping):
                return {
                    "status": "INCOMPLETE",
                    "reason": f"codex-telemetry usage is missing for {record['id']}",
                    "missing": [f"{record['id']}: codex_usage"],
                }
            usage_invalid = [
                field
                for field in ("input_tokens", "output_tokens")
                if (
                    isinstance(usage.get(field), bool)
                    or not isinstance(usage.get(field), (int, float))
                    or not math.isfinite(float(usage[field]))
                    or float(usage[field]) < 0
                )
            ]
            if usage_invalid:
                return {
                    "status": "INVALID",
                    "reason": f"codex-telemetry usage is invalid for {record['id']}",
                    "fields": [f"codex_usage.{field}" for field in usage_invalid],
                }
        values.append(value)
    if missing:
        return {
            "status": "INCOMPLETE",
            "matched_tasks": len(values),
            "required_tasks": len(eligible_records),
            "missing": missing,
        }

    baseline = sum(float(value["baseline_codex_tokens"]) for value in values)
    triage_tokens = sum(
        float(value.get("triage_codex_tokens", 0)) for value in values
    )
    delegation = sum(
        float(value.get("triage_codex_tokens", 0))
        + float(value["delegation_codex_tokens"])
        + float(value["review_codex_tokens"])
        + float(value["repair_codex_tokens"])
        + float(value["recovery_codex_tokens"])
        for value in values
    )
    saved = baseline - delegation
    baseline_seconds = sum(float(value["baseline_seconds"]) for value in values)
    delegated_seconds = sum(
        float(record["duration_seconds"])
        + float(value.get("triage_seconds", 0))
        + float(value["delegation_seconds"])
        + float(value["review_seconds"])
        + float(value["repair_seconds"])
        + float(value["recovery_seconds"])
        for record, value in zip(eligible_records, values)
    )
    review_seconds = sum(float(value["review_seconds"]) for value in values)
    repair_seconds = sum(float(value["repair_seconds"]) for value in values)
    recovery_seconds = sum(float(value["recovery_seconds"]) for value in values)
    delegation_tokens = sum(float(value["delegation_codex_tokens"]) for value in values)
    review_tokens = sum(float(value["review_codex_tokens"]) for value in values)
    repair_tokens = sum(float(value["repair_codex_tokens"]) for value in values)
    recovery_tokens = sum(float(value["recovery_codex_tokens"]) for value in values)
    delegation_seconds = sum(float(value["delegation_seconds"]) for value in values)
    triage_seconds = sum(float(value.get("triage_seconds", 0)) for value in values)
    report = {
        # Character/pass ledgers and manually repriced packets are useful
        # estimates, but they are not provider telemetry. Keep the arithmetic
        # in the report while preventing an estimate from satisfying the
        # measured-economics MVP gate.
        "status": "MEASURED" if source == "codex-telemetry" else "ESTIMATED",
        "matched_tasks": len(values),
        "baseline_codex_tokens": baseline,
        "delegated_codex_tokens": delegation,
        "triage_codex_tokens": triage_tokens,
        "net_codex_tokens_saved": saved,
        "net_codex_token_reduction": saved / baseline if baseline else None,
        "frontier_token_leverage": saved / delegation if delegation else None,
        "delegation_codex_tokens": delegation_tokens,
        "review_codex_tokens": review_tokens,
        "repair_codex_tokens": repair_tokens,
        "recovery_codex_tokens": recovery_tokens,
        "baseline_seconds": baseline_seconds,
        "delegated_seconds": delegated_seconds,
        "codex_delegation_seconds": delegation_seconds,
        "codex_triage_seconds": triage_seconds,
        "codex_review_seconds": review_seconds,
        "codex_repair_seconds": repair_seconds,
        "codex_recovery_seconds": recovery_seconds,
        "wall_clock_overhead": (
            (delegated_seconds - baseline_seconds) / baseline_seconds
            if baseline_seconds
            else None
        ),
        "source": source,
        "cohort": dict(cohort),
    }
    if triage_unpriced:
        report["warnings"] = [
            "Triage decision cost was not supplied for every task; legacy economics may be optimistic.",
            f"Missing optional triage fields: {len(triage_unpriced)}",
        ]
    if cohort_warnings:
        report.setdefault("warnings", []).extend(cohort_warnings)
    return report


def _economics_task(economics: Mapping[str, Any] | None, case_id: str) -> Mapping[str, Any]:
    if not isinstance(economics, Mapping):
        return {}
    tasks = economics.get("tasks", {})
    if not isinstance(tasks, Mapping):
        return {}
    value = tasks.get(case_id, {})
    return value if isinstance(value, Mapping) else {}


def _apply_economics_attestations(
    records: list[dict[str, Any]],
    economics: Mapping[str, Any] | None,
) -> None:
    """Apply per-task review attestations when a completed run is resumed.

    A resumed checkpoint already contains the model result, so the execution
    loop is skipped.  Economics still supplies the authoritative scope and
    repair classification used by aggregate metrics; without this overlay a
    resumed report silently turns those fields back into ``None``.
    """

    if economics is None:
        return
    for record in records:
        task_economics = _economics_task(economics, str(record.get("id", "")))
        if not task_economics:
            continue
        if "scope_reviewed" in task_economics:
            record["scope_reviewed"] = task_economics["scope_reviewed"]
            record["scope_review_basis"] = "economics attestation"
        if "scope_violation" in task_economics:
            scope_violation = task_economics["scope_violation"] is True
            record["scope_violation"] = scope_violation
            if scope_violation:
                record["status"] = ResultStatus.SCOPE_VIOLATION.value
                record["passed"] = False
        if "substantial_codex_repair" in task_economics:
            record["substantial_codex_repair"] = task_economics[
                "substantial_codex_repair"
            ]


def _gate_status(
    metrics: Mapping[str, Any],
    economics: Mapping[str, Any],
    task_count: int,
    invalid_fixture_count: int = 0,
    run_state: str = "COMPLETE",
) -> dict[str, Any]:
    checks: dict[str, bool | None] = {
        "run_completed": run_state == "COMPLETE",
        "benchmark_has_at_least_50_tasks": task_count >= 50 and invalid_fixture_count == 0,
        "blocked_task_cohort_present": metrics["blocked_expected_tasks"] > 0,
        "blocked_task_correctness": (
            metrics["blocked_task_correctness"] == 1.0
            if metrics["blocked_task_correctness"] is not None else None
        ),
        "bounded_acceptance": (
            metrics["bounded_acceptance_rate"] >= 0.80
            if metrics["bounded_acceptance_rate"] is not None else None
        ),
        "verification_pass_rate": (
            metrics["verification_pass_rate"] >= 0.85
            if metrics["verification_pass_rate"] is not None else None
        ),
        "scope_violation_rate": (
            metrics["scope_violation_rate"] < 0.01
            if metrics["scope_violation_rate"] is not None and metrics["scope_review_complete"] else None
        ),
        "substantial_codex_repair_rate": (
            metrics["substantial_codex_repair_rate"] < 0.15
            if metrics["substantial_codex_repair_rate"] is not None else None
        ),
        "net_codex_token_reduction": (
            economics.get("net_codex_token_reduction") >= 0.50
            if economics.get("status") == "MEASURED" and economics.get("net_codex_token_reduction") is not None
            else None
        ),
        "wall_clock_overhead": (
            economics.get("wall_clock_overhead") <= 0.25
            if economics.get("status") == "MEASURED" and economics.get("wall_clock_overhead") is not None
            else None
        ),
    }
    if any(value is False for value in checks.values()):
        overall = "FAIL"
    elif any(value is None for value in checks.values()):
        overall = "NOT_EVALUATED"
    else:
        overall = "PASS"
    return {"overall": overall, "checks": checks}


def _tree_digest(root: Path, directories: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    files: list[Path] = []
    for directory in directories:
        if not directory.exists():
            continue
        files.extend(
            path for path in directory.rglob("*")
            if path.is_file()
            and ".git" not in path.parts
            and "evals\\results" not in str(path.relative_to(root)).replace("/", "\\")
            and "__pycache__" not in path.parts
            and ".pytest_cache" not in path.parts
            and path.suffix != ".pyc"
        )
    for path in sorted(set(files), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _repository_identity(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        revision = completed.stdout.strip()
        if completed.returncode == 0 and revision:
            return f"git:{revision}"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "working-tree:" + _tree_digest(root, [root])


def _local_runtime(result: Any) -> dict[str, Any]:
    attempts = result.metadata.get("attempt_history", [])
    runtime = [item.get("local_runtime", {}) for item in attempts if item.get("local_runtime")]
    if not runtime and result.metadata.get("worker_runtime"):
        runtime = [result.metadata["worker_runtime"]]
    numeric_keys = (
        "prompt_eval_count",
        "eval_count",
        "total_duration",
        "load_duration",
        "prompt_eval_duration",
        "eval_duration",
        "codex_wall_clock_seconds",
        "stdout_bytes",
        "stderr_bytes",
    )
    values: dict[str, Any] = {key: 0 for key in numeric_keys}
    input_tokens = 0
    output_tokens = 0
    telemetry_observed = False
    reported_zero_usage = False
    providers: set[str] = set()
    result_sources: set[str] = set()
    for item in runtime:
        for key in numeric_keys:
            value = item.get(key)
            if isinstance(value, (int, float)):
                values[key] += value
        usage = item.get("usage")
        if isinstance(usage, Mapping):
            input_value = usage.get("input_tokens")
            output_value = usage.get("output_tokens")
            if isinstance(input_value, (int, float)) and not isinstance(input_value, bool):
                input_tokens += input_value
                telemetry_observed = True
            if isinstance(output_value, (int, float)) and not isinstance(output_value, bool):
                output_tokens += output_value
                telemetry_observed = True
            if input_value == 0 and output_value == 0:
                reported_zero_usage = True
        if isinstance(item.get("prompt_eval_count"), (int, float)):
            input_tokens += item["prompt_eval_count"]
            telemetry_observed = True
        if isinstance(item.get("eval_count"), (int, float)):
            output_tokens += item["eval_count"]
            telemetry_observed = True
        if isinstance(item.get("provider"), str):
            providers.add(item["provider"])
        if isinstance(item.get("result_source"), str):
            result_sources.add(item["result_source"])
    values["model"] = runtime[-1].get("model") if runtime else None
    values["attempts"] = len(runtime)
    values["wall_clock_seconds"] = result.duration_seconds
    values["input_tokens"] = input_tokens
    values["output_tokens"] = output_tokens
    values["telemetry_source"] = (
        "provider-telemetry"
        if telemetry_observed and not reported_zero_usage
        else "provider-reported-zero"
        if reported_zero_usage
        else "unavailable"
    )
    values["providers"] = sorted(providers)
    values["result_sources"] = sorted(result_sources)
    values["codex_version"] = runtime[-1].get("codex_version") if runtime else None
    values["model_pull_detected"] = any(
        item.get("model_pull_detected") is True for item in runtime
    )
    return values


def _compact_verification(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    compact: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, Mapping):
            continue
        value: dict[str, Any] = {
            key: item[key]
            for key in ("command", "exit_code", "passed")
            if key in item
        }
        if item.get("timed_out"):
            value["timed_out"] = True
        if item.get("passed") is False:
            detail = item.get("stderr") or item.get("stdout") or ""
            value["failure_tail"] = str(detail)[-240:]
        compact.append(value)
    return compact


def _compact_case_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return name or "case"


def _compact_records(
    records: list[Mapping[str, Any]],
    artifact_root: Path,
) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for record in records:
        case_id = str(record["id"])
        patch = str(record.get("patch") or "")
        patch_path = artifact_root / f"{_compact_case_name(case_id)}.patch"
        patch_path.write_text(patch, encoding="utf-8")
        value: dict[str, Any] = {
            "id": case_id,
            "task_id": record.get("task_id"),
            "eligibility": record.get("eligibility"),
            "status": record.get("status"),
            "passed": record.get("passed"),
            "files_changed": record.get("files_changed", []),
            "verification": _compact_verification(record.get("verification")),
            "patch_artifact": patch_path.name,
            "patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
            "patch_bytes": len(patch.encode("utf-8")),
        }
        if record.get("attempts") != 1:
            value["attempts"] = record.get("attempts")
        if record.get("scope_violation"):
            value["scope_violation"] = True
        if record.get("scope_reviewed") is not None:
            value["scope_reviewed"] = record.get("scope_reviewed")
        if record.get("scope_review_basis"):
            value["scope_review_basis"] = str(record["scope_review_basis"])[:180]
        scope_review = record.get("scope_review")
        if isinstance(scope_review, Mapping):
            value["scope_review"] = {
                "reviewed": scope_review.get("reviewed"),
                "violation": scope_review.get("violation"),
                "paths": scope_review.get("paths", []),
                "hunks": scope_review.get("hunks", 0),
                "reasons": [
                    str(item)[:180]
                    for item in scope_review.get("reasons", [])[:2]
                ],
            }
        if record.get("substantial_codex_repair") is not None:
            value["substantial_codex_repair"] = record.get(
                "substantial_codex_repair"
            )
        if record.get("blocked_result_correct"):
            value["blocked_result_correct"] = True
        if record.get("main_worktree_unchanged") is not True:
            value["main_worktree_unchanged"] = record.get(
                "main_worktree_unchanged"
            )
        if record.get("reasons"):
            value["reasons"] = list(record["reasons"])[:3]
        if record.get("blockers"):
            value["blockers"] = list(record["blockers"])[:3]
        runtime = record.get("local_runtime")
        if isinstance(runtime, Mapping):
            value["local_runtime"] = {
                key: runtime[key]
                for key in (
                    "model",
                    "attempts",
                    "telemetry_source",
                    "providers",
                    "result_sources",
                    "codex_version",
                    "model_pull_detected",
                )
                if key in runtime
            }
        compact.append(value)
    return compact


def _proof_record(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return the minimum per-case proof needed in an aggregate handoff.

    The complete compact record remains in ``full-records.json`` and the patch
    remains in its named artifact. The frontier packet only needs enough
    information to decide which artifacts to open and why a case did not pass.
    Keeping the failure/sample proof small is a real token saving, while the
    deterministic checks and independent artifacts preserve reviewability.
    """

    proof: dict[str, Any] = {
        "id": value.get("id"),
        "status": value.get("status"),
        "passed": value.get("passed"),
        "patch_artifact": value.get("patch_artifact"),
    }
    eligibility = value.get("eligibility")
    if eligibility != "eligible":
        proof["eligibility"] = eligibility
    files_changed = value.get("files_changed")
    if files_changed:
        proof["files_changed"] = list(files_changed)

    verification = value.get("verification")
    if isinstance(verification, list) and verification:
        proof_verification: list[dict[str, Any]] = []
        for item in verification:
            if not isinstance(item, Mapping):
                continue
            compact_item = {
                key: item[key]
                for key in ("command", "exit_code", "passed", "timed_out")
                if key in item
            }
            if item.get("passed") is False and item.get("failure_tail"):
                compact_item["failure_tail"] = str(item["failure_tail"])[-160:]
            proof_verification.append(compact_item)
        if proof_verification:
            proof["verification"] = proof_verification

    attempts = value.get("attempts")
    if attempts not in (None, 1):
        proof["attempts"] = attempts
    for key in ("scope_violation", "scope_reviewed", "blocked_result_correct"):
        if value.get(key) is not None and value.get(key) is not False:
            proof[key] = value[key]
    if value.get("main_worktree_unchanged") is not None:
        proof["main_worktree_unchanged"] = value["main_worktree_unchanged"]
    for key in ("reasons", "blockers"):
        values = value.get(key)
        if isinstance(values, list) and values:
            proof[key] = [str(item)[:180] for item in values[:2]]
    runtime = value.get("local_runtime")
    if isinstance(runtime, Mapping):
        runtime_proof = {
            key: runtime[key]
            for key in ("result_sources", "model_pull_detected", "codex_version")
            if key in runtime and runtime[key] not in (None, [], False)
        }
        if runtime_proof:
            proof["local_runtime"] = runtime_proof
    return proof


def run_suite(
    *,
    backend: str,
    model: str | None,
    suite: str,
    repo_root: str | Path,
    output_path: str | Path | None = None,
    economics_path: str | Path | None = None,
    compact: bool = False,
    artifact_dir: str | Path | None = None,
    aggregate: bool = False,
    sample: int = 0,
    manifest_mode: str = "full",
    checkpoint_path: str | Path | None = None,
    resume: bool = False,
    max_cases: int | None = None,
) -> dict[str, Any]:
    if sample < 0:
        raise ValueError("sample must be nonnegative")
    if max_cases is not None and (
        isinstance(max_cases, bool) or not isinstance(max_cases, int) or max_cases <= 0
    ):
        raise ValueError("max_cases must be a positive integer when provided")
    if resume and checkpoint_path is None:
        raise ValueError("resume requires checkpoint_path")
    if manifest_mode not in MANIFEST_MODES:
        raise ValueError(
            f"manifest_mode must be one of {', '.join(MANIFEST_MODES)}"
        )
    if backend not in {"ollama", "codex-ollama", "fixture"}:
        raise ValueError(f"unsupported backend: {backend}")
    repo_path = Path(repo_root).resolve()
    evals_root = repo_path / "evals"
    cases_root = evals_root / "cases"
    fixtures_root = evals_root / "fixtures"
    if not (cases_root / f"{suite}.json").is_file():
        raise FileNotFoundError(
            f"suite {suite!r} was not found under {evals_root}; --repo must contain evals/"
        )
    suite_data = _load_suite(suite, cases_root)
    cases = list(suite_data["cases"])
    case_ids = [str(case.get("id", "")) for case in cases]
    task_ids = [str(case.get("task", {}).get("task_id", "")) for case in cases]
    if len(set(case_ids)) != len(case_ids) or any(not item for item in case_ids):
        raise ValueError("evaluation suite contains duplicate or empty case ids")
    if len(set(task_ids)) != len(task_ids) or any(not item for item in task_ids):
        raise ValueError("evaluation suite contains duplicate or empty task ids")
    _validate_declared_fixture_patches(
        cases,
        cases_root=cases_root,
        fixtures_root=fixtures_root,
    )
    fixture_digest = _tree_digest(repo_path, [cases_root, fixtures_root])
    repository_identity = _repository_identity(repo_path)
    runtime_config = OllamaConfig.from_env() if backend == "ollama" else None
    codex_config = (
        CodexCliConfig.from_env() if backend == "codex-ollama" else None
    )
    selected_model = model or (
        runtime_config.default_model
        if runtime_config is not None
        else codex_config.default_model
        if codex_config is not None
        else None
    )
    ollama_version = None
    runtime_probe_error = None
    if runtime_config is not None:
        try:
            ollama_version = OllamaClient(runtime_config).version()
        except Exception as exc:
            runtime_probe_error = str(exc)
    elif codex_config is not None:
        try:
            ollama_version = OllamaClient(
                OllamaConfig(
                    host=codex_config.ollama_host,
                    timeout_seconds=min(codex_config.timeout_seconds, 15.0),
                )
            ).version()
        except Exception as exc:
            runtime_probe_error = str(exc)
    economics: Mapping[str, Any] | None = None
    if economics_path is not None:
        economics_value = json.loads(Path(economics_path).read_text(encoding="utf-8"))
        if not isinstance(economics_value, Mapping):
            raise ValueError("economics file must contain a JSON object")
        economics = economics_value
    checkpoint = (
        Path(checkpoint_path).resolve() if checkpoint_path is not None else None
    )
    records: list[dict[str, Any]] = []
    if resume:
        assert checkpoint is not None
        if not checkpoint.is_file():
            raise FileNotFoundError(
                f"cannot resume evaluation; checkpoint does not exist: {checkpoint}"
            )
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        if not isinstance(saved, Mapping):
            raise ValueError("evaluation checkpoint must contain a JSON object")
        if saved.get("suite") != suite or saved.get("backend") != backend:
            raise ValueError(
                "checkpoint suite/backend does not match the requested evaluation"
            )
        if saved.get("case_ids") != case_ids:
            raise ValueError(
                "checkpoint case IDs do not match the current evaluation suite"
            )
        saved_records = saved.get("cases", [])
        if not isinstance(saved_records, list):
            raise ValueError("evaluation checkpoint cases must be a list")
        if len(saved_records) > len(cases):
            raise ValueError("evaluation checkpoint contains too many cases")
        for index, record in enumerate(saved_records):
            if not isinstance(record, Mapping) or record.get("id") != case_ids[index]:
                raise ValueError(
                    "evaluation checkpoint must contain a contiguous case prefix"
                )
            records.append(dict(record))
        saved_runtime = saved.get("runtime")
        if isinstance(saved_runtime, Mapping):
            saved_fixture_digest = saved_runtime.get("fixture_digest")
            if (
                isinstance(saved_fixture_digest, str)
                and saved_fixture_digest != fixture_digest
            ):
                raise ValueError(
                    "checkpoint fixture digest does not match the current suite"
                )
            saved_identity = saved_runtime.get("repository_identity")
            if isinstance(saved_identity, str) and saved_identity:
                repository_identity = saved_identity
        _apply_economics_attestations(records, economics)
    checkpoint_runtime = {
        "host": (
            runtime_config.host
            if runtime_config is not None
            else codex_config.ollama_host
            if codex_config is not None
            else None
        ),
        "provider": backend,
        "model": selected_model,
        "local_provider": (
            codex_config.local_provider if codex_config is not None else None
        ),
        "codex_executable": (
            codex_config.executable if codex_config is not None else None
        ),
        "codex_timeout_seconds": (
            codex_config.timeout_seconds if codex_config is not None else None
        ),
        "codex_idle_timeout_seconds": (
            codex_config.idle_timeout_seconds if codex_config is not None else None
        ),
        "codex_temperature": (
            codex_config.ollama_temperature if codex_config is not None else None
        ),
        "codex_seed": (
            codex_config.ollama_seed if codex_config is not None else None
        ),
        "fixture_digest": fixture_digest,
        "repository_identity": repository_identity,
    }

    def write_checkpoint(
        state: str,
        *,
        error: BaseException | None = None,
        final_report: Mapping[str, Any] | None = None,
    ) -> None:
        if checkpoint is None:
            return
        payload: dict[str, Any] = {
            "run_state": state,
            "suite": suite,
            "backend": backend,
            "model": selected_model,
            "total_cases": len(cases),
            "completed_cases": len(records),
            "case_ids": case_ids,
            "runtime": checkpoint_runtime,
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "cases": records,
        }
        if error is not None:
            payload["error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
        if final_report is not None:
            payload["report"] = final_report
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        temporary = checkpoint.with_name(f".{checkpoint.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, checkpoint)

    write_checkpoint("RUNNING")
    all_passed = all(record.get("passed") is True for record in records)
    run_state = "COMPLETE"
    try:
        stop_index = len(cases)
        if max_cases is not None:
            stop_index = min(len(cases), len(records) + max_cases)
        for case in cases[len(records):stop_index]:
            fixture = fixtures_root / case["fixture"]
            task = DelegationTask.from_dict(case["task"])
            started = time.perf_counter()
            if backend == "fixture":
                worker = FixtureWorker(case, cases_root)
                result = delegate_local(
                    task=task,
                    repo=fixture,
                    worker=worker,
                )
            elif backend == "ollama":
                config = runtime_config or OllamaConfig.from_env()
                if model:
                    config = OllamaConfig(
                        host=config.host,
                        default_model=model,
                        api_key=config.api_key,
                        timeout_seconds=config.timeout_seconds,
                        temperature=config.temperature,
                        num_predict=config.num_predict,
                        think=config.think,
                        seed=config.seed,
                    )
                result = delegate_local(
                    task=task,
                    repo=fixture,
                    model=model,
                    client=OllamaClient(config),
                )
            else:
                result = delegate_local(
                    task=task,
                    repo=fixture,
                    model=model,
                    worker=CodexCliWorker(
                        repo=fixture,
                        model=selected_model,
                        config=codex_config,
                    ),
                )
            case_passed, reasons = _case_passed(case, result)
            if case["eligibility"] == "invalid_fixture":
                reasons.append("invalid_fixture cases are excluded from the benchmark gate")
                case_passed = False
            if (
                case["eligibility"] == "blocked_expected"
                and result.status is ResultStatus.BLOCKED
            ):
                task_review: Mapping[str, Any] = {
                    "reviewed": True,
                    "violation": False,
                    "basis": "expected blocked refusal; no patch delegated",
                    "reasons": [],
                    "paths": [],
                    "hunks": 0,
                }
            else:
                expected_patch = None
                patch_name = case.get("patch_file")
                if isinstance(patch_name, str) and patch_name.strip():
                    expected_patch = (cases_root / patch_name).read_text(
                        encoding="utf-8"
                    )
                task_review = review_task_patch(
                    result.patch,
                    task,
                    repository=fixture,
                    expected_files=case.get("expected_files", []),
                    expected_patch=expected_patch,
                )
                if task_review.get("violation") is True:
                    reasons.append(
                        "task-aware scope review failed: "
                        + "; ".join(str(item) for item in task_review.get("reasons", []))
                    )
                    case_passed = False
            task_economics = _economics_task(economics, case["id"])
            repair_value = task_economics.get("substantial_codex_repair")
            if "scope_reviewed" in task_economics:
                scope_reviewed = task_economics["scope_reviewed"]
            else:
                scope_reviewed = (
                    task_review.get("reviewed")
                    if case["eligibility"] == "eligible"
                    else True
                    if case["eligibility"] == "blocked_expected"
                    and result.status is ResultStatus.BLOCKED
                    else None
                )
            scope_violation = (
                result.status is ResultStatus.SCOPE_VIOLATION
                or task_review.get("violation") is True
                or task_economics.get("scope_violation") is True
            )
            all_passed = all_passed and case_passed
            records.append({
                "id": case["id"],
                "task_id": task.task_id,
                "category": case.get("category"),
                "difficulty": case.get("difficulty"),
                "eligibility": case["eligibility"],
                "status": (
                    ResultStatus.SCOPE_VIOLATION.value
                    if scope_violation
                    else result.status.value
                ),
                "passed": case_passed,
                "reasons": reasons,
                "files_changed": list(result.files_changed),
                "attempts": result.attempts,
                "verification_passed": (
                    result.status is ResultStatus.SUCCESS
                    and all(item.passed for item in result.verification)
                ),
                "first_attempt_accepted": case_passed and result.attempts == 1,
                "bounded_acceptance": case_passed,
                "scope_violation": scope_violation,
                "scope_reviewed": scope_reviewed if isinstance(scope_reviewed, bool) else None,
                "scope_review_basis": (
                    "economics attestation"
                    if "scope_reviewed" in task_economics
                    else task_review.get("basis")
                ),
                "scope_review": dict(task_review),
                "substantial_codex_repair": (
                    repair_value if isinstance(repair_value, bool) else None
                ),
                "blocked_result_correct": (
                    case["eligibility"] == "blocked_expected"
                    and result.status is ResultStatus.BLOCKED
                ),
                "duration_seconds": time.perf_counter() - started,
                "blockers": list(result.blockers),
                "sandbox_mode": result.sandbox_mode,
                "main_worktree_unchanged": result.metadata.get("main_worktree_unchanged"),
                "attempt_history": result.metadata.get("attempt_history", []),
                "context_chars": result.metadata.get("context_chars"),
                "local_runtime": _local_runtime(result),
                "verification": [item.to_dict() for item in result.verification],
                "patch": result.patch,
            })
            write_checkpoint("RUNNING")
            if (
                backend == "codex-ollama"
                and records[-1]["local_runtime"].get("model_pull_detected") is True
            ):
                all_passed = False
                run_state = "ABORTED"
                write_checkpoint(
                    "ABORTED",
                    error=RuntimeError(
                        "Codex CLI attempted an implicit Ollama model pull; "
                        "the suite stopped before repeating setup failure"
                    ),
                )
                break
        if run_state == "COMPLETE" and len(records) < len(cases):
            run_state = "PARTIAL"
    except BaseException as exc:
        run_state = "ABORTED" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "ERROR"
        write_checkpoint(run_state, error=exc)
        raise
    eligible_records = [record for record in records if record["eligibility"] == "eligible"]
    cohort = _cohort_identity(
        backend=backend,
        suite=suite,
        fixture_digest=fixture_digest,
        repository_identity=repository_identity,
        selected_model=selected_model,
        cases=cases,
        codex_config=codex_config,
        runtime_config=runtime_config,
        records=records,
    )
    metrics = _aggregate(cases, records)
    economics_report = _economics_report(
        eligible_records,
        economics,
        cohort,
    )
    valid_task_count = sum(
        case["eligibility"] in {"eligible", "blocked_expected"} for case in cases
    )
    invalid_fixture_count = sum(
        case["eligibility"] == "invalid_fixture" for case in cases
    )
    report = {
        "run_state": run_state,
        "suite": suite,
        "backend": backend,
        "model": selected_model,
        "cohort": cohort,
        "status": (
            run_state
            if run_state != "COMPLETE"
            else "PASS"
            if all_passed
            else "FAIL"
        ),
        "cases": records,
        "runtime": {
            "host": (
                runtime_config.host
                if runtime_config is not None
                else codex_config.ollama_host
                if codex_config is not None
                else None
            ),
            "model": selected_model,
            "provider": backend,
            "local_provider": (
                codex_config.local_provider if codex_config is not None else None
            ),
            "codex_executable": (
                codex_config.executable if codex_config is not None else None
            ),
            "codex_reasoning_effort": (
                codex_config.reasoning_effort if codex_config is not None else None
            ),
            "codex_timeout_seconds": (
                codex_config.timeout_seconds if codex_config is not None else None
            ),
            "codex_idle_timeout_seconds": (
                codex_config.idle_timeout_seconds if codex_config is not None else None
            ),
            "codex_retry_model": (
                codex_config.retry_model if codex_config is not None else None
            ),
            "codex_temperature": (
                codex_config.ollama_temperature if codex_config is not None else None
            ),
            "codex_seed": (
                codex_config.ollama_seed if codex_config is not None else None
            ),
            "codex_sandbox": (
                codex_config.sandbox if codex_config is not None else None
            ),
            "codex_require_model_present": (
                codex_config.require_model_present if codex_config is not None else None
            ),
            "codex_probe_version": (
                codex_config.probe_version if codex_config is not None else None
            ),
            "temperature": runtime_config.temperature if runtime_config else None,
            "seed": runtime_config.seed if runtime_config else None,
            "num_predict": runtime_config.num_predict if runtime_config else None,
            "think": runtime_config.think if runtime_config else None,
            "ollama_version": ollama_version,
            "benchmark_date_utc": datetime.now(timezone.utc).isoformat(),
            "quantization": os.environ.get("OLLAMA_QUANTIZATION") if runtime_config else None,
            "hardware": os.environ.get("OLLAMA_HARDWARE") if runtime_config else None,
            "context_limit": os.environ.get("OLLAMA_CONTEXT_LIMIT") if runtime_config else None,
            "runtime_probe_error": runtime_probe_error,
            "fixture_digest": fixture_digest,
            "repository_identity": repository_identity,
        },
        "metrics": metrics,
        "economics": economics_report,
        "mvp_gate": _gate_status(
            metrics,
            economics_report,
            valid_task_count,
            invalid_fixture_count,
            run_state=run_state,
        ),
    }
    if backend == "fixture":
        report["notice"] = (
            "Fixture backend validates orchestration and sandbox behavior only; "
            "it is not evidence of local-model quality or Codex token savings."
        )
    if compact or aggregate:
        # Preserve the counterfactual full delegated response before replacing
        # it with the frontier-facing proof packet. This is a response-size
        # baseline, not a Codex-only implementation baseline.
        full_report_payload: dict[str, Any] = {
            **report,
            "cases": records,
        }
        artifact_root = (
            Path(artifact_dir).resolve()
            if artifact_dir is not None
            else Path(tempfile.mkdtemp(prefix="ar-eval-artifacts-"))
        )
        artifact_root.mkdir(parents=True, exist_ok=True)
        evidence_path = artifact_root / "full-records.json"
        evidence_path.write_text(
            json.dumps(
                {"suite": suite, "backend": backend, "records": records},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        compact_cases = _compact_records(records, artifact_root)
        report["review_mode"] = "aggregate-proof" if aggregate else "compact-handoff"
        report["artifact_dir"] = str(artifact_root)
        report["full_evidence_artifact"] = evidence_path.name
        report["all_main_worktrees_unchanged"] = all(
            record.get("main_worktree_unchanged") is True for record in records
        )
        if aggregate:
            report["cases"] = []
            # Keep the denominator and every case identity while avoiding a
            # repeated object envelope for every passing task. Detailed
            # records remain in full-records.json and failures/sample entries
            # below retain their verification evidence.
            report["case_index"] = {
                "passed": [
                    item["id"] for item in compact_cases if item["passed"] is True
                ],
                "failed": [
                    item["id"] for item in compact_cases if item["passed"] is not True
                ],
            }
            status_counts: dict[str, int] = {}
            for item in compact_cases:
                status = str(item.get("status"))
                status_counts[status] = status_counts.get(status, 0) + 1
            report["case_status_counts"] = status_counts
            report["case_failures"] = [
                _proof_record(item)
                for item in compact_cases
                if item.get("passed") is not True
            ]
            eligible_sample = [
                item
                for item in compact_cases
                if item.get("eligibility") == "eligible"
                and item.get("passed") is True
            ][:sample]
            report["case_review_sample"] = [
                _proof_record(item) for item in eligible_sample
            ]
            report["review_sample_count"] = len(eligible_sample)
            report["review_policy"] = (
                "deterministic gates plus failure review and deterministic passing "
                "sample review"
                if sample > 0
                else "failure review only; passing tasks require external "
                "full-evidence review or a nonzero sample"
            )
        else:
            report["cases"] = compact_cases
        review_artifacts = (
            [item["patch_artifact"] for item in compact_cases]
            if not aggregate
            else [
                item["patch_artifact"]
                for item in (
                    report["case_failures"] + report["case_review_sample"]
                )
            ]
        )
        manifest_payload = task_manifest(cases, manifest_mode)
        report["frontier_manifest_mode"] = manifest_mode
        # These fields are included in the packet and are updated below after
        # the budget metadata has been attached.
        report["frontier_handoff_chars"] = 0
        report["frontier_handoff_tokens_estimate"] = 0
        frontier_budget(
            full_payload=full_report_payload,
            compact_payload=report,
            artifact_root=artifact_root,
            review_artifacts=review_artifacts,
            task_manifest=manifest_payload,
            task_manifest_mode=manifest_mode,
        )
        for _ in range(3):
            serialized = json.dumps(
                report,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            report["frontier_handoff_chars"] = len(serialized)
            report["frontier_handoff_tokens_estimate"] = token_estimate(serialized)
            budget = report["frontier_budget"]
            if isinstance(budget, Mapping):
                budget["compact_handoff_tokens_estimate"] = report[
                    "frontier_handoff_tokens_estimate"
                ]
                full_tokens = int(budget.get("full_report_tokens_estimate", 0))
                budget["response_compaction_reduction_estimate"] = (
                    (full_tokens - report["frontier_handoff_tokens_estimate"])
                    / full_tokens
                    if full_tokens
                    else None
                )
                review_tokens = int(
                    budget.get("review_artifact_tokens_estimate", 0)
                )
                manifest_tokens = int(
                    budget.get("task_manifest_tokens_estimate") or 0
                )
                budget["frontier_tokens_with_selected_review_estimate"] = (
                    report["frontier_handoff_tokens_estimate"] + review_tokens
                )
                budget["selected_review_reduction_estimate"] = (
                    (
                        full_tokens
                        - report["frontier_handoff_tokens_estimate"]
                        - review_tokens
                    )
                    / full_tokens
                    if full_tokens
                    else None
                )
                budget["frontier_tokens_with_manifest_and_review_estimate"] = (
                    report["frontier_handoff_tokens_estimate"]
                    + review_tokens
                    + manifest_tokens
                )
    if output_path is not None:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_checkpoint(run_state, final_report=report)
    return report


def run_codex_baseline_suite(**kwargs: Any) -> dict[str, Any]:
    """Run the direct Codex baseline without coupling it to the Ollama lane."""

    from .codex_baseline import run_codex_baseline_suite as _run_baseline

    return _run_baseline(**kwargs)


def reprice_frontier_economics(
    *,
    run_report_path: str | Path,
    economics_path: str | Path,
    repo_root: str | Path,
    artifact_dir: str | Path | None = None,
    sample: int = 5,
    manifest_mode: str = "full",
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Reprice an existing matched run for the compact Codex frontier path.

    This intentionally does not execute a model. It combines a previously
    recorded full run and its matched Codex-only economics input with the
    current proof-packet policy. The result is an explicitly labelled estimate
    of the batched frontier cost; it is not provider telemetry and cannot turn
    an unmatched or missing baseline into measured savings.
    """

    if sample < 0:
        raise ValueError("sample must be nonnegative")
    if manifest_mode not in MANIFEST_MODES:
        raise ValueError(
            f"manifest_mode must be one of {', '.join(MANIFEST_MODES)}"
        )
    run_path = Path(run_report_path).resolve()
    economics_file = Path(economics_path).resolve()
    repo_path = Path(repo_root).resolve()
    run_value = json.loads(run_path.read_text(encoding="utf-8"))
    economics_value = json.loads(economics_file.read_text(encoding="utf-8"))
    if not isinstance(run_value, Mapping):
        raise ValueError("run report must contain a JSON object")
    if not isinstance(economics_value, Mapping):
        raise ValueError("economics file must contain a JSON object")
    raw_records = run_value.get("cases")
    if not isinstance(raw_records, list) or not raw_records:
        raise ValueError("run report must contain full cases for repricing")
    records = [dict(item) for item in raw_records if isinstance(item, Mapping)]
    if len(records) != len(raw_records):
        raise ValueError("run report cases must all be objects")

    suite = str(run_value.get("suite") or "")
    backend = str(run_value.get("backend") or "")
    model = str(run_value.get("model") or "<none>")
    if not suite or not backend:
        raise ValueError("run report must contain suite and backend")
    cases_root = repo_path / "evals" / "cases"
    fixtures_root = repo_path / "evals" / "fixtures"
    suite_data = _load_suite(suite, cases_root)
    cases = list(suite_data["cases"])
    case_ids = {str(case.get("id")) for case in cases}
    record_ids = {str(record.get("id")) for record in records}
    if case_ids != record_ids:
        raise ValueError("run report cases do not match the current suite")

    runtime = run_value.get("runtime")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    current_fixture_digest = _tree_digest(repo_path, [cases_root, fixtures_root])
    recorded_fixture_digest = runtime.get("fixture_digest")
    if (
        isinstance(recorded_fixture_digest, str)
        and recorded_fixture_digest
        and recorded_fixture_digest != current_fixture_digest
    ):
        raise ValueError(
            "run report fixture digest does not match the current suite/fixtures"
        )

    expected_cohort = {
        "backend": backend,
        "suite": suite,
        "fixture_digest": str(recorded_fixture_digest or current_fixture_digest),
        "repository_identity": str(runtime.get("repository_identity") or ""),
        "model": model,
    }
    if not expected_cohort["repository_identity"]:
        raise ValueError("run report runtime is missing repository_identity")
    eligible_records = [
        record for record in records if record.get("eligibility") == "eligible"
    ]
    matched = _economics_report(
        eligible_records,
        economics_value,
        expected_cohort,
    )
    if matched.get("status") not in {"MEASURED", "ESTIMATED"}:
        return {
            "status": matched.get("status", "INCOMPLETE"),
            "reason": "matched economics could not be repriced",
            "matched_economics": matched,
            "run_report": str(run_path),
            "economics": str(economics_file),
        }

    artifact_root = (
        Path(artifact_dir).resolve()
        if artifact_dir is not None
        else Path(tempfile.mkdtemp(prefix="ar-reprice-artifacts-"))
    )
    artifact_root.mkdir(parents=True, exist_ok=True)
    evidence_path = artifact_root / "full-records.json"
    evidence_path.write_text(
        json.dumps(
            {"suite": suite, "backend": backend, "records": records},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    compact_cases = _compact_records(records, artifact_root)
    failures = [item for item in compact_cases if item.get("passed") is not True]
    passing_sample = [
        item
        for item in compact_cases
        if item.get("eligibility") == "eligible" and item.get("passed") is True
    ][:sample]
    status_counts: dict[str, int] = {}
    for item in compact_cases:
        status = str(item.get("status"))
        status_counts[status] = status_counts.get(status, 0) + 1
    compact_payload: dict[str, Any] = {
        "status": run_value.get("status"),
        "suite": suite,
        "backend": backend,
        "model": model,
        "task_count": len(cases),
        "frontier_manifest_mode": manifest_mode,
        "metrics": _aggregate(cases, records),
        "matched_baseline": {
            "baseline_codex_tokens": matched["baseline_codex_tokens"],
            "source": matched.get("source"),
        },
        "review_mode": "aggregate-proof",
        "artifact_dir": str(artifact_root),
        "full_evidence_artifact": evidence_path.name,
        "all_main_worktrees_unchanged": all(
            record.get("main_worktree_unchanged") is True for record in records
        ),
        "case_index": {
            "passed": [item["id"] for item in compact_cases if item["passed"] is True],
            "failed": [item["id"] for item in compact_cases if item["passed"] is not True],
        },
        "case_status_counts": status_counts,
        "case_failures": failures,
        "case_review_sample": passing_sample,
        "review_sample_count": len(passing_sample),
        "review_policy": (
            "deterministic gates plus failure review and deterministic passing "
            "sample review"
            if sample > 0
            else "failure review only; passing tasks require external "
            "full-evidence review or a nonzero sample"
        ),
        "runtime": {
            key: runtime[key]
            for key in (
                "provider",
                "local_provider",
                "codex_version",
                "ollama_version",
                "benchmark_date_utc",
                "fixture_digest",
                "repository_identity",
            )
            if key in runtime
        },
    }
    review_artifacts = [
        item["patch_artifact"]
        for item in (*failures, *passing_sample)
    ]
    manifest_payload = task_manifest(cases, manifest_mode)
    full_payload = dict(run_value)
    repair_tokens = matched["repair_codex_tokens"]
    recovery_tokens = matched["recovery_codex_tokens"]
    triage_tokens = float(matched.get("triage_codex_tokens", 0))
    repriced: dict[str, Any] = {}
    for _ in range(3):
        compact_payload["frontier_handoff_chars"] = 0
        compact_payload["frontier_handoff_tokens_estimate"] = 0
        frontier_budget(
            full_payload=full_payload,
            compact_payload=compact_payload,
            artifact_root=artifact_root,
            review_artifacts=review_artifacts,
            task_manifest=manifest_payload,
            task_manifest_mode=manifest_mode,
        )
        budget = compact_payload["frontier_budget"]
        if not isinstance(budget, Mapping):
            raise RuntimeError("frontier budget did not produce a mapping")
        manifest_tokens = float(budget.get("task_manifest_tokens_estimate") or 0)
        handoff_tokens = float(budget["compact_handoff_tokens_estimate"])
        review_tokens = float(budget["review_artifact_tokens_estimate"])
        delegated_tokens = (
            manifest_tokens
            + handoff_tokens
            + review_tokens
            + triage_tokens
            + repair_tokens
            + recovery_tokens
        )
        baseline_tokens = float(matched["baseline_codex_tokens"])
        saved_tokens = baseline_tokens - delegated_tokens
        repriced = {
            "status": "ESTIMATED",
            "source": "estimate-repriced-frontier-packet",
            "baseline_codex_tokens": baseline_tokens,
            "delegated_codex_tokens": delegated_tokens,
            "net_codex_tokens_saved": saved_tokens,
            "net_codex_token_reduction": (
                saved_tokens / baseline_tokens if baseline_tokens else None
            ),
            "frontier_token_leverage": (
                saved_tokens / delegated_tokens if delegated_tokens else None
            ),
            "components": {
                "task_manifest_or_decomposition_tokens": manifest_tokens,
                "triage_decision_tokens": triage_tokens,
                "compact_handoff_tokens": handoff_tokens,
                "selected_review_artifact_tokens": review_tokens,
                "repair_codex_tokens": repair_tokens,
                "recovery_codex_tokens": recovery_tokens,
            },
            "matched_economics": {
                "source": matched.get("source"),
                "cohort": matched.get("cohort"),
                "warnings": matched.get("warnings", []),
                "original_delegated_codex_tokens": matched[
                    "delegated_codex_tokens"
                ],
                "original_review_codex_tokens": matched["review_codex_tokens"],
            },
            "caveats": [
                "This reprices the recorded matched run; it does not execute a new Codex-only baseline.",
                "The task manifest is conservatively counted once as frontier decomposition input.",
                "Triage decision cost is included only when supplied by the matched economics record.",
                "Repair and recovery costs are carried from the matched economics record.",
                "All values are estimates unless provider telemetry is explicitly recorded.",
            ],
        }
        compact_payload["frontier_repriced_economics"] = repriced

    for _ in range(3):
        serialized = json.dumps(
            compact_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        compact_payload["frontier_handoff_chars"] = len(serialized)
        compact_payload["frontier_handoff_tokens_estimate"] = token_estimate(serialized)
        budget = compact_payload["frontier_budget"]
        if isinstance(budget, Mapping):
            full_tokens = int(budget.get("full_report_tokens_estimate", 0))
            handoff_tokens = compact_payload["frontier_handoff_tokens_estimate"]
            review_tokens = int(budget.get("review_artifact_tokens_estimate", 0))
            manifest_tokens = int(budget.get("task_manifest_tokens_estimate") or 0)
            budget["compact_handoff_tokens_estimate"] = handoff_tokens
            budget["frontier_tokens_with_selected_review_estimate"] = (
                handoff_tokens + review_tokens
            )
            budget["response_compaction_reduction_estimate"] = (
                (full_tokens - handoff_tokens) / full_tokens if full_tokens else None
            )
            budget["selected_review_reduction_estimate"] = (
                (full_tokens - handoff_tokens - review_tokens) / full_tokens
                if full_tokens
                else None
            )
            budget["frontier_tokens_with_manifest_and_review_estimate"] = (
                handoff_tokens + review_tokens + manifest_tokens
            )
        # Recalculate repricing after the packet accounting fields settle.
        if isinstance(budget, Mapping):
            manifest_tokens = float(budget.get("task_manifest_tokens_estimate") or 0)
            handoff_tokens = float(budget.get("compact_handoff_tokens_estimate", 0))
            review_tokens = float(budget.get("review_artifact_tokens_estimate", 0))
            delegated_tokens = (
                manifest_tokens
                + handoff_tokens
                + review_tokens
                + triage_tokens
                + repair_tokens
                + recovery_tokens
            )
            baseline_tokens = float(matched["baseline_codex_tokens"])
            repriced["delegated_codex_tokens"] = delegated_tokens
            repriced["net_codex_tokens_saved"] = baseline_tokens - delegated_tokens
            repriced["net_codex_token_reduction"] = (
                (baseline_tokens - delegated_tokens) / baseline_tokens
                if baseline_tokens
                else None
            )
            repriced["frontier_token_leverage"] = (
                (baseline_tokens - delegated_tokens) / delegated_tokens
                if delegated_tokens
                else None
            )
            repriced["components"].update({
                "task_manifest_or_decomposition_tokens": manifest_tokens,
                "triage_decision_tokens": triage_tokens,
                "compact_handoff_tokens": handoff_tokens,
                "selected_review_artifact_tokens": review_tokens,
            })

    final_budget = compact_payload["frontier_budget"]
    final_handoff_tokens = float(
        final_budget.get("compact_handoff_tokens_estimate", 0)
    )
    final_review_tokens = float(
        final_budget.get("review_artifact_tokens_estimate", 0)
    )
    baseline_tokens = float(matched["baseline_codex_tokens"])
    manifest_sensitivity: dict[str, Any] = {}
    for mode in MANIFEST_MODES:
        mode_manifest = task_manifest(cases, mode)
        mode_manifest_tokens = float(
            token_estimate(mode_manifest) if mode_manifest is not None else 0
        )
        mode_total = (
            mode_manifest_tokens
            + final_handoff_tokens
            + final_review_tokens
            + triage_tokens
            + repair_tokens
            + recovery_tokens
        )
        mode_saved = baseline_tokens - mode_total
        manifest_sensitivity[mode] = {
            "manifest_tokens": mode_manifest_tokens,
            "delegated_codex_tokens": mode_total,
            "net_codex_token_reduction": (
                mode_saved / baseline_tokens if baseline_tokens else None
            ),
            "frontier_token_leverage": (
                mode_saved / mode_total if mode_total else None
            ),
        }
    repriced["manifest_mode"] = manifest_mode
    repriced["manifest_sensitivity"] = manifest_sensitivity

    result = {
        "status": "ESTIMATED",
        "run_report": str(run_path),
        "economics": str(economics_file),
        "artifact_dir": str(artifact_root),
        "full_evidence_artifact": evidence_path.name,
        "frontier_budget": compact_payload["frontier_budget"],
        "frontier_repriced_economics": repriced,
        "frontier_packet": compact_payload,
    }
    if output_path is not None:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--backend",
        choices=("ollama", "codex-ollama", "fixture"),
        default="ollama",
    )
    parser.add_argument("--model")
    parser.add_argument("--suite", default="bounded-basic")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--economics", type=Path)
    args = parser.parse_args()
    report = run_suite(
        backend=args.backend,
        model=args.model,
        suite=args.suite,
        repo_root=args.repo,
        output_path=args.output,
        economics_path=args.economics,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
