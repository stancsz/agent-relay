"""Run a matched direct-Codex baseline for the bounded evaluation corpus.

This module intentionally does not reuse the Ollama worker.  The baseline must
exercise the normal Codex provider and retain its ``turn.completed`` usage
events so the delegated lane can be priced against a real frontier run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Mapping, Sequence

from agent_relay.delegate import collect_context
from agent_relay.patch import (
    capture_diff,
    changed_files,
    validate_patch_scope,
)
from agent_relay.result import ResultStatus
from agent_relay.sandbox import GitSandbox, SandboxError
from agent_relay.task import DelegationTask
from agent_relay.verifier import run_verification
from evals.scope_review import review_task_patch


@dataclass(frozen=True)
class CodexBaselineConfig:
    executable: str
    model: str | None = None
    timeout_seconds: float = 180.0


def resolve_codex_executable(value: str | None = None) -> str:
    """Resolve an explicit baseline binary without silently switching lanes."""

    candidate = (
        value
        or os.environ.get("LCD_BASELINE_CODEX_BIN")
        or os.environ.get("LCD_CODEX_BIN")
        or shutil.which("codex.cmd")
        or shutil.which("codex")
    )
    if not candidate:
        raise FileNotFoundError(
            "Codex CLI was not found; pass --codex-bin or set "
            "LCD_BASELINE_CODEX_BIN"
        )
    return candidate


def _json_events(text: str) -> list[Mapping[str, Any]]:
    events: list[Mapping[str, Any]] = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            events.append(value)
    return events


def usage_from_events(text: str) -> dict[str, Any]:
    """Sum Codex ``turn.completed`` usage without treating missing as zero."""

    totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "turns": 0,
    }
    usage_seen = False
    nonzero_seen = False
    for event in _json_events(text):
        if event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, Mapping):
            continue
        totals["turns"] += 1
        for key in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        ):
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] += value
                usage_seen = True
                nonzero_seen = nonzero_seen or value > 0
    totals["token_status"] = (
        "provider-telemetry"
        if usage_seen and nonzero_seen
        else "provider-reported-zero"
        if usage_seen
        else "unavailable"
    )
    totals["total_tokens"] = (
        totals["input_tokens"] + totals["output_tokens"]
        if usage_seen
        else None
    )
    return totals


def _compact_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return name or "case"


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(value)


def _baseline_prompt(task: DelegationTask, context: str) -> str:
    sections = [
        "You are the direct Codex-only baseline worker for one bounded coding task.",
        "Work in the current disposable Git repository using your tools.",
        "Implement only this task. Do not make architecture decisions, unrelated cleanup, commits, or changes outside the declared write boundary.",
        f"Task ID: {task.task_id}",
        "Objective:\n" + task.objective,
        "Allowed write files:\n" + "\n".join(f"- {item}" for item in task.allowed_files),
    ]
    if task.context:
        sections.append(
            "Declared context ranges and excerpts (write only inside ranges for "
            "allowed files; entries from other files are read-only):\n"
            + "\n".join(f"- {item}" for item in task.context)
        )
    if task.context_mode == "insert_after":
        sections.append(
            "Edit mode: insert only the requested new definitions after the declared "
            "context range. Preserve the existing context and do not rewrite unrelated "
            "tests or top-level code."
        )
    if task.requirements:
        sections.append(
            "Requirements:\n" + "\n".join(f"- {item}" for item in task.requirements)
        )
    if task.constraints:
        sections.append(
            "Constraints:\n" + "\n".join(f"- {item}" for item in task.constraints)
        )
    sections.append(
        "Verification commands to run:\n"
        + "\n".join(f"- {item}" for item in task.verification)
    )
    if task.success_criteria:
        sections.append(
            "Success criteria:\n"
            + "\n".join(f"- {item}" for item in task.success_criteria)
        )
    sections.extend(
        [
            "Read-only task context:\n" + context,
            "Make the smallest valid change, run every declared verification command, and stop.",
            "Return a short factual final message describing the changed files and verification. Do not include a long transcript.",
        ]
    )
    return "\n\n".join(sections)


def _run_case(
    case: Mapping[str, Any],
    *,
    cases_root: Path,
    fixtures_root: Path,
    artifact_root: Path,
    config: CodexBaselineConfig,
) -> dict[str, Any]:
    case_id = str(case["id"])
    task = DelegationTask.from_dict(case["task"])
    fixture = fixtures_root / str(case["fixture"])
    case_artifact = artifact_root / _compact_name(case_id)
    case_artifact.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    stdout = ""
    stderr = ""
    final_message = ""
    return_code: int | None = None
    timed_out = False
    scope_violation = False
    files: tuple[str, ...] = ()
    patch = ""
    verification: tuple[Any, ...] = ()
    sandbox_mode: str | None = None
    failure: str | None = None
    scope_review: dict[str, Any] = {
        "reviewed": False,
        "violation": False,
        "basis": "patch unavailable; path/context review incomplete",
        "reasons": ["patch is empty"],
        "paths": [],
        "hunks": 0,
    }

    try:
        with GitSandbox(fixture, f"codex-baseline-{task.task_id}") as sandbox:
            if sandbox.path is None:
                raise SandboxError("baseline sandbox did not expose a path")
            sandbox_mode = sandbox.mode
            context = collect_context(sandbox.path, task)
            final_path = sandbox.path / ".lcd-baseline-final-message.txt"
            command = [
                config.executable,
                "exec",
                "--json",
                "--ignore-user-config",
                "--ephemeral",
                "--sandbox",
                "danger-full-access",
                "--cd",
                str(sandbox.path),
                "--output-last-message",
                str(final_path),
            ]
            if config.model:
                command.extend(["--model", config.model])
            process_started = time.perf_counter()
            try:
                completed = subprocess.run(
                    command,
                    input=_baseline_prompt(task, context),
                    cwd=sandbox.path,
                    text=True,
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=config.timeout_seconds,
                    check=False,
                )
                return_code = completed.returncode
                stdout = completed.stdout or ""
                stderr = completed.stderr or ""
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                stdout = exc.stdout if isinstance(exc.stdout, str) else ""
                stderr = exc.stderr if isinstance(exc.stderr, str) else ""
                failure = f"Codex baseline timed out after {config.timeout_seconds:g} seconds"
            process_seconds = time.perf_counter() - process_started
            if final_path.is_file():
                final_message = final_path.read_text(
                    encoding="utf-8", errors="replace"
                ).strip()
                final_path.unlink(missing_ok=True)
            sandbox.clean_verification_artifacts()
            patch = capture_diff(sandbox.path)
            files = changed_files(sandbox.path)
            if patch.strip():
                try:
                    validate_patch_scope(patch, task.allowed_files)
                except Exception as exc:
                    scope_violation = True
                    failure = str(exc)
                scope_review = review_task_patch(
                    patch,
                    task,
                    repository=fixture,
                    expected_files=case.get("expected_files", []),
                    expected_patch=(
                        (cases_root / str(case["patch_file"])).read_text(
                            encoding="utf-8"
                        )
                        if isinstance(case.get("patch_file"), str)
                        else None
                    ),
                )
                if scope_review.get("violation") is True:
                    scope_violation = True
                    failure = failure or "; ".join(
                        str(item) for item in scope_review.get("reasons", [])
                    )
            verification = run_verification(task.verification, sandbox.path)
            sandbox.clean_verification_artifacts()
    except (OSError, SandboxError, ValueError) as exc:
        failure = str(exc)
        process_seconds = 0.0

    usage = usage_from_events(stdout)
    stdout_path = case_artifact / "codex.stdout.jsonl"
    stderr_path = case_artifact / "codex.stderr.log"
    message_path = case_artifact / "final-message.txt"
    patch_path = case_artifact / "result.patch"
    _write_text(stdout_path, stdout)
    _write_text(stderr_path, stderr)
    _write_text(message_path, final_message)
    _write_text(patch_path, patch)

    if timed_out:
        status = ResultStatus.TIMEOUT
    elif scope_violation:
        status = ResultStatus.SCOPE_VIOLATION
    elif return_code not in (None, 0):
        status = ResultStatus.WORKER_ERROR
    elif not files:
        status = ResultStatus.WORKER_ERROR
        failure = failure or "Codex completed without a changed file"
    elif not all(item.passed for item in verification):
        status = ResultStatus.FAILED_VERIFICATION
    else:
        status = ResultStatus.SUCCESS

    expected_files = sorted(str(item) for item in case.get("expected_files", []))
    actual_files = sorted(files)
    reasons: list[str] = []
    if status.value != str(case.get("expected_status", "SUCCESS")):
        reasons.append(
            f"expected {case.get('expected_status', 'SUCCESS')}, got {status.value}"
        )
    if expected_files != actual_files:
        reasons.append(f"expected files {expected_files}, got {actual_files}")
    if not all(item.passed for item in verification):
        reasons.append("one or more verification commands failed")
    if failure:
        reasons.append(failure[:1000])
    passed = not reasons
    return {
        "id": case_id,
        "task_id": task.task_id,
        "category": case.get("category"),
        "difficulty": case.get("difficulty"),
        "eligibility": case.get("eligibility"),
        "status": status.value,
        "passed": passed,
        "bounded_acceptance": passed,
        "verification_passed": all(item.passed for item in verification),
        "first_attempt_accepted": passed,
        "scope_violation": scope_violation,
        "scope_reviewed": (
            scope_review.get("reviewed")
            if isinstance(scope_review.get("reviewed"), bool)
            else None
        ),
        "scope_review_basis": scope_review.get("basis"),
        "scope_review": scope_review,
        "substantial_codex_repair": None,
        "attempts": 1,
        "duration_seconds": time.perf_counter() - started,
        "codex_seconds": process_seconds,
        "return_code": return_code,
        "sandbox_mode": sandbox_mode,
        "files_changed": list(files),
        "expected_files": expected_files,
        "main_worktree_unchanged": True,
        "reasons": reasons,
        "failure": failure,
        "final_message": final_message[:1000],
        "verification": [item.to_dict() for item in verification],
        "patch_artifact": str(patch_path),
        "stdout_artifact": str(stdout_path),
        "stderr_artifact": str(stderr_path),
        "final_message_artifact": str(message_path),
        "patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
        "patch_bytes": len(patch.encode("utf-8")),
        "codex_usage": usage,
    }


def run_codex_baseline_suite(
    *,
    suite: str,
    repo_root: str | Path,
    model: str | None = None,
    codex_bin: str | None = None,
    output_path: str | Path | None = None,
    artifact_dir: str | Path | None = None,
    checkpoint_path: str | Path | None = None,
    resume: bool = False,
    max_cases: int | None = None,
    timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    """Run the direct Codex baseline with resumable, reviewable evidence."""

    if max_cases is not None and (
        isinstance(max_cases, bool)
        or not isinstance(max_cases, int)
        or max_cases <= 0
    ):
        raise ValueError("max_cases must be positive when provided")
    if resume and checkpoint_path is None:
        raise ValueError("resume requires checkpoint_path")

    from .runner import (
        _load_suite,
        _repository_identity,
        _tree_digest,
        _validate_declared_fixture_patches,
    )

    repo = Path(repo_root).resolve()
    cases_root = repo / "evals" / "cases"
    fixtures_root = repo / "evals" / "fixtures"
    suite_data = _load_suite(suite, cases_root)
    cases = list(suite_data["cases"])
    _validate_declared_fixture_patches(
        cases,
        cases_root=cases_root,
        fixtures_root=fixtures_root,
    )
    case_ids = [str(case["id"]) for case in cases]
    executable = resolve_codex_executable(codex_bin)
    config = CodexBaselineConfig(
        executable=executable,
        model=model,
        timeout_seconds=timeout_seconds,
    )
    artifact_root = (
        Path(artifact_dir).resolve()
        if artifact_dir is not None
        else Path(tempfile.mkdtemp(prefix="lcd-codex-baseline-"))
    )
    artifact_root.mkdir(parents=True, exist_ok=True)
    checkpoint = Path(checkpoint_path).resolve() if checkpoint_path else None
    records: list[dict[str, Any]] = []
    if resume:
        assert checkpoint is not None
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        if saved.get("suite") != suite or saved.get("backend") != "codex-only":
            raise ValueError("baseline checkpoint suite/backend does not match")
        if saved.get("case_ids") != case_ids:
            raise ValueError("baseline checkpoint case IDs do not match")
        saved_records = saved.get("cases", [])
        if not isinstance(saved_records, list) or len(saved_records) > len(cases):
            raise ValueError("baseline checkpoint cases are invalid")
        for index, record in enumerate(saved_records):
            if not isinstance(record, Mapping) or record.get("id") != case_ids[index]:
                raise ValueError("baseline checkpoint must contain a contiguous prefix")
            records.append(dict(record))

    fixture_digest = _tree_digest(repo, [cases_root, fixtures_root])
    repository_identity = _repository_identity(repo)
    try:
        version_result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        codex_version = (version_result.stdout or version_result.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        codex_version = f"unavailable: {exc}"

    def write_checkpoint(state: str, report: Mapping[str, Any] | None = None) -> None:
        if checkpoint is None:
            return
        payload: dict[str, Any] = {
            "run_state": state,
            "backend": "codex-only",
            "suite": suite,
            "model": model,
            "codex_executable": executable,
            "codex_version": codex_version,
            "case_ids": case_ids,
            "completed_cases": len(records),
            "total_cases": len(cases),
            "fixture_digest": fixture_digest,
            "repository_identity": repository_identity,
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "cases": records,
        }
        if report is not None:
            payload["report"] = report
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        temporary = checkpoint.with_name(f".{checkpoint.name}.tmp")
        _write_text(temporary, json.dumps(payload, ensure_ascii=False, indent=2))
        os.replace(temporary, checkpoint)

    write_checkpoint("RUNNING")
    state = "COMPLETE"
    stop_index = len(cases)
    if max_cases is not None:
        stop_index = min(len(cases), len(records) + max_cases)
    try:
        for case in cases[len(records) : stop_index]:
            if case.get("eligibility") == "blocked_expected":
                records.append({
                    "id": case["id"],
                    "task_id": case["task"]["task_id"],
                    "eligibility": "blocked_expected",
                    "status": "BLOCKED",
                    "passed": True,
                    "bounded_acceptance": False,
                    "verification_passed": False,
                    "first_attempt_accepted": False,
                    "blocked_result_correct": True,
                    "scope_violation": False,
                    "scope_reviewed": True,
                    "substantial_codex_repair": False,
                    "attempts": 0,
                    "duration_seconds": 0.0,
                    "codex_usage": {"token_status": "not_delegated"},
                })
            elif case.get("eligibility") == "invalid_fixture":
                records.append({
                    "id": case["id"],
                    "task_id": case["task"]["task_id"],
                    "eligibility": "invalid_fixture",
                    "status": "WORKER_ERROR",
                    "passed": False,
                    "reasons": ["invalid_fixture is excluded from baseline"],
                })
            else:
                records.append(
                    _run_case(
                        case,
                        cases_root=cases_root,
                        fixtures_root=fixtures_root,
                        artifact_root=artifact_root,
                        config=config,
                    )
                )
            write_checkpoint("RUNNING")
        if len(records) < len(cases):
            state = "PARTIAL"
    except BaseException as exc:
        state = "ERROR"
        write_checkpoint(state, {"error": f"{type(exc).__name__}: {exc}"})
        raise

    eligible = [record for record in records if record.get("eligibility") == "eligible"]
    passed = sum(record.get("passed") is True for record in eligible)
    verified = sum(record.get("verification_passed") is True for record in eligible)
    usage_records = [
        record.get("codex_usage", {})
        for record in eligible
        if isinstance(record.get("codex_usage"), Mapping)
    ]
    telemetry_complete = bool(usage_records) and all(
        value.get("token_status") == "provider-telemetry" for value in usage_records
    )
    total_input = sum(float(value.get("input_tokens", 0)) for value in usage_records)
    total_output = sum(float(value.get("output_tokens", 0)) for value in usage_records)
    report: dict[str, Any] = {
        "run_state": state,
        "backend": "codex-only",
        "suite": suite,
        "model": model,
        "status": "PASS" if state == "COMPLETE" and passed == len(eligible) else "FAIL" if state == "COMPLETE" else state,
        "cohort": {
            "backend": "codex-only",
            "suite": suite,
            "fixture_digest": fixture_digest,
            "repository_identity": repository_identity,
            "model": model or "<configured>",
            "case_ids": case_ids,
            "codex_executable": executable,
            "codex_version": codex_version,
        },
        "runtime": {
            "codex_executable": executable,
            "codex_version": codex_version,
            "timeout_seconds": timeout_seconds,
            "fixture_digest": fixture_digest,
            "repository_identity": repository_identity,
        },
        "metrics": {
            "eligible_tasks": len(eligible),
            "bounded_acceptances": passed,
            "verification_passes": verified,
            "bounded_acceptance_rate": passed / len(eligible) if eligible else None,
            "verification_pass_rate": verified / len(eligible) if eligible else None,
            "scope_violations": sum(record.get("scope_violation") is True for record in eligible),
            "scope_review_complete": False,
            "codex_usage_complete": telemetry_complete,
        },
        "codex_usage": {
            "status": "MEASURED" if telemetry_complete else "INCOMPLETE",
            "input_tokens": total_input,
            "output_tokens": total_output,
            "total_tokens": total_input + total_output,
            "eligible_tasks": len(eligible),
        },
        "cases": records,
        "artifact_dir": str(artifact_root),
        "notice": "This is the matched direct-Codex baseline. It does not itself price delegated parent review or frontier handoff cost.",
    }
    write_checkpoint(state, report)
    if output_path is not None:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        _write_text(output, json.dumps(report, ensure_ascii=False, indent=2))
    return report
