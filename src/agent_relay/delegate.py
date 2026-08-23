from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re
import time
from typing import Any, Iterable, Sequence

from .codex_worker import CodexCliError, _recover_code_block_patch
from .ollama import OllamaClient, OllamaError
from .patch import (
    append_diff,
    append_hunk_diff,
    append_patch_diff,
    PatchError,
    ScopeViolationError,
    apply_patch,
    capture_diff,
    check_patch,
    changed_files,
    ranged_full_file_diff,
    ranged_replacement_diff,
    replacement_diff,
    normalize_patch_transport,
    normalize_single_file_hunk,
    normalize_relative_path,
    rebase_single_file_hunk,
    rebase_unified_patch,
    validate_patch_scope,
    worktree_status,
)
from .result import DelegationResult, ResultStatus, VerificationResult
from .sandbox import GitSandbox, SandboxError
from .task import (
    DelegationTask,
    TaskContractError,
    context_path_and_range,
)
from .verifier import run_verification
from .worker import OllamaWorker, RetryEvidence


class DelegationError(RuntimeError):
    """Raised for orchestration failures that should become a result."""


def _strip_code_fence(value: str) -> str:
    candidate = value.strip()
    lines = candidate.splitlines()
    if len(lines) >= 2 and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip("\n")
    return value


def _response_candidate(response: Any) -> str:
    """Serialize the bounded candidate so a retry can repair the real output."""

    candidate: dict[str, Any] = {}
    patch = getattr(response, "patch", "")
    if isinstance(patch, str) and patch.strip():
        candidate["patch"] = patch
    file_contents = getattr(response, "file_contents", ())
    if file_contents:
        candidate["files"] = {
            path: content for path, content in file_contents
        }
    if not candidate:
        return ""
    return json.dumps(candidate, ensure_ascii=False, indent=2)


def _worker_failure_detail(error: BaseException) -> str:
    """Keep retry evidence useful without forwarding an unbounded transcript."""

    detail = str(error)[:2000]
    runtime = getattr(error, "runtime", {})
    if isinstance(runtime, dict):
        preview = runtime.get("final_message_preview")
        if isinstance(preview, str) and preview.strip():
            detail += "\nPrevious final response preview:\n" + preview[:2400]
    return detail


def _append_only_requested(task: DelegationTask) -> bool:
    text = " ".join(
        (
            task.objective,
            *task.requirements,
            *task.constraints,
            *task.success_criteria,
        )
    ).lower()
    return "append" in text


def _normalize_reported_diff_headers(
    patch: str,
    allowed_files: Sequence[str],
) -> str:
    """Canonicalize a one-file diff fragment before applying it.

    Small local models sometimes omit the space in ``+++ path`` or omit the
    leading ``diff --git`` header entirely.  Those are transport defects, not
    permission to broaden the write scope.  Normalize only the two file-header
    lines and add a deterministic Git header when the task has one allowed
    file; the normal scope and apply gates still reject anything else.
    """

    lines = patch.splitlines()
    if not lines:
        return patch
    hunk_seen = False
    old_seen = False
    new_seen = False
    normalized: list[str] = []
    for line in lines:
        if line.startswith("@@ "):
            hunk_seen = True
        if not hunk_seen and line.startswith("---") and not old_seen:
            value = line[3:].strip()
            if value:
                normalized.append("--- " + value)
                old_seen = True
                continue
        if not hunk_seen and line.startswith("+++") and not new_seen:
            value = line[3:].strip()
            if value:
                normalized.append("+++ " + value)
                new_seen = True
                continue
        normalized.append(line)

    candidate = "\n".join(normalized)
    if patch.endswith(("\n", "\r")):
        candidate += "\n"
    if (
        old_seen
        and new_seen
        and not any(line.startswith("diff --git ") for line in normalized)
        and len(allowed_files) == 1
    ):
        path = normalize_relative_path(allowed_files[0])
        candidate = (
            f"diff --git a/{path} b/{path}\n"
            + candidate
        )
    return candidate


def _coerce_worker_patch(response: Any, sandbox_path: Path, task: DelegationTask) -> str:
    ranged_context = any(":" in spec for spec in task.context)
    file_contents = dict(getattr(response, "file_contents", ()) or ())
    if file_contents:
        if ranged_context:
            try:
                return ranged_replacement_diff(
                    sandbox_path,
                    file_contents,
                    task.allowed_files,
                    task.context,
                    context_mode=task.context_mode,
                )
            except PatchError as snippet_error:
                try:
                    return ranged_full_file_diff(
                        sandbox_path,
                        file_contents,
                        task.allowed_files,
                        task.context,
                        context_mode=task.context_mode,
                    )
                except PatchError:
                    raise snippet_error
        return replacement_diff(sandbox_path, file_contents, task.allowed_files)

    # The current Codex Responses lane can return a useful diff or target
    # definition inside prose while its structured envelope is empty or its
    # first hunk is stale. Re-run the bounded code-block recovery against the
    # raw response before applying a line-only candidate. The helper is limited
    # to the one allowed file and declared range, then git/apply verification
    # remains mandatory below.
    raw_response = getattr(response, "raw_response", "")
    if ranged_context and isinstance(raw_response, str) and raw_response.strip():
        recovered = _recover_code_block_patch(sandbox_path, task, raw_response)
        if recovered:
            return recovered

    patch = getattr(response, "patch", "")
    if not isinstance(patch, str) or not patch.strip():
        raise PatchError("worker returned no patch or replacement files")
    stripped = _strip_code_fence(patch)
    diff_candidate = stripped.lstrip()
    patch_lines = diff_candidate.splitlines()
    has_old_header = any(line.startswith("---") for line in patch_lines)
    has_new_header = any(line.startswith("+++") for line in patch_lines)
    if (
        diff_candidate.startswith("diff --git ")
        or (has_old_header and has_new_header)
    ):
        candidate = _normalize_reported_diff_headers(
            diff_candidate if diff_candidate.endswith("\n") else diff_candidate + "\n",
            task.allowed_files,
        )
        candidate = normalize_patch_transport(candidate)
        candidate_valid = True
        try:
            check_patch(sandbox_path, candidate)
        except PatchError:
            candidate_valid = False
            candidate = rebase_unified_patch(sandbox_path, candidate)
            try:
                check_patch(sandbox_path, candidate)
            except PatchError:
                pass
            else:
                candidate_valid = True
        if _append_only_requested(task) and not candidate_valid:
            try:
                return append_patch_diff(
                    sandbox_path,
                    candidate,
                    task.allowed_files,
                )
            except PatchError:
                pass
        return candidate
    if len(task.allowed_files) == 1 and diff_candidate.startswith("@@ "):
        # Some local coding models emit a valid single-file hunk but omit the
        # standard file headers. Add only the deterministic headers; the outer
        # scope validator and verifier still decide whether it is acceptable.
        path = task.allowed_files[0]
        if _append_only_requested(task):
            try:
                return append_hunk_diff(
                    sandbox_path,
                    path,
                    diff_candidate,
                    task.allowed_files,
                )
            except PatchError:
                pass
        candidate = (
            f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            f"{diff_candidate if diff_candidate.endswith(chr(10)) else diff_candidate + chr(10)}"
        )
        normalized_hunk = rebase_single_file_hunk(
            sandbox_path,
            path,
            normalize_single_file_hunk(diff_candidate),
        )
        return normalize_patch_transport(
            candidate.replace(diff_candidate, normalized_hunk, 1)
        )
    if (
        len(task.allowed_files) == 1
        and not ranged_context
        and _append_only_requested(task)
    ):
        try:
            return append_diff(
                sandbox_path,
                {task.allowed_files[0]: stripped},
                task.allowed_files,
            )
        except PatchError:
            pass
    if ranged_context:
        if len(task.allowed_files) == 1:
            try:
                return ranged_full_file_diff(
                    sandbox_path,
                    {task.allowed_files[0]: stripped},
                    task.allowed_files,
                    task.context,
                    context_mode=task.context_mode,
                )
            except PatchError:
                pass
        raise PatchError(
            "worker returned unsupported content for ranged context; return a "
            "unified diff or one target-range snippet"
        )
    if len(task.allowed_files) != 1:
        raise PatchError(
            "worker returned complete content without a unified diff for a multi-file task"
        )
    return replacement_diff(
        sandbox_path,
        {task.allowed_files[0]: stripped},
        task.allowed_files,
    )


def _safe_snapshot(repo: Path) -> tuple[str, ...] | None:
    try:
        return worktree_status(repo)
    except PatchError:
        return None


def _read_context_file(repo: Path, relative_path: str) -> tuple[str, bool]:
    root = repo.resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise TaskContractError(
            f"context path resolves outside repository: {relative_path!r}"
        ) from exc
    if not candidate.exists():
        return "[missing file]", False
    if not candidate.is_file():
        return "[context path is not a file]", False
    return candidate.read_text(encoding="utf-8", errors="replace"), True


def collect_context(
    repo: str | Path,
    task: DelegationTask,
    *,
    max_file_chars: int = 40000,
    max_total_chars: int = 120000,
) -> str:
    root = Path(repo).resolve()
    specs = task.context or task.allowed_files
    sections: list[str] = []
    total = 0
    for spec in specs:
        path, start, end = context_path_and_range(spec)
        content, readable = _read_context_file(root, path)
        full_allowed_context = False
        if start is not None and readable:
            lines = content.splitlines()
            if start > len(lines) or (end is not None and end > len(lines)):
                raise TaskContractError(
                    f"context range {spec!r} is outside file {path!r}"
                )
            # A small local model cannot reliably construct a patch from only
            # a signature-sized slice of a larger allowed file. Keep the range
            # as the write boundary, but provide the complete allowed file as
            # grounded read-only context so hunk locations and neighboring
            # definitions are not guessed. Read-only assertion files remain
            # range-limited.
            normalized_path = normalize_relative_path(path)
            normalized_allowed = {
                normalize_relative_path(item) for item in task.allowed_files
            }
            if normalized_path not in normalized_allowed:
                content = "\n".join(lines[start - 1 : end])
            else:
                full_allowed_context = True
        if len(content) > max_file_chars:
            content = content[:max_file_chars] + "\n...[file context truncated]"
        section = f"--- {path}"
        if start is not None:
            if full_allowed_context:
                section += f" (full file; write target lines {start}"
                if end is not None and end != start:
                    section += f"-{end}"
                section += ")"
            else:
                section += f":{start}"
                if end is not None and end != start:
                    section += f"-{end}"
        section += f" ---\n{content}"
        if total + len(section) > max_total_chars:
            remaining = max_total_chars - total
            if remaining <= 0:
                break
            section = section[:remaining] + "\n...[context truncated]"
        sections.append(section)
        total += len(section)
        if total >= max_total_chars:
            break
    return "\n\n".join(sections) or "[no readable context provided]"


def _failure_summary(
    verification: Iterable[VerificationResult],
) -> tuple[str, tuple[dict[str, Any], ...]]:
    values = tuple(item.to_dict() for item in verification)
    failed = [item for item in verification if not item.passed]
    if not failed:
        return "verification passed", values
    fragments = []
    for item in failed:
        detail = (item.stderr or item.stdout).strip().replace("\n", " ")
        fragments.append(
            f"{item.command} exited {item.exit_code}: {detail[:400]}"
        )
    return "; ".join(fragments), values


def _result(
    *,
    task: DelegationTask,
    status: ResultStatus,
    started: float,
    summary: str = "",
    files_changed: Sequence[str] = (),
    patch: str = "",
    verification: Sequence[VerificationResult] = (),
    blockers: Sequence[str] = (),
    attempts: int = 0,
    sandbox_mode: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> DelegationResult:
    receipt_metadata = dict(metadata or {})
    receipt_metadata.setdefault("lane", "local-qwen")
    return DelegationResult(
        task_id=task.task_id,
        status=status,
        summary=summary,
        files_changed=tuple(files_changed),
        patch=patch,
        verification=tuple(verification),
        blockers=tuple(blockers),
        attempts=attempts,
        duration_seconds=time.perf_counter() - started,
        sandbox_mode=sandbox_mode,
        metadata=receipt_metadata,
    )


def _merge_initial_attempt_history(
    result: DelegationResult,
    initial_attempt_history: Sequence[dict[str, Any]],
) -> DelegationResult:
    """Keep pre-sandbox worker failures in the final accounting packet.

    The initial Codex/Ollama call happens before the disposable worktree is
    created, while verification attempts are recorded inside the sandbox.
    Without merging these histories, a retryable provider failure would be
    invisible to eval economics and the frontier would undercount review and
    recovery effort.
    """

    if not initial_attempt_history:
        return result
    metadata = dict(result.metadata)
    merged = [dict(item) for item in initial_attempt_history]
    existing = metadata.get("attempt_history", [])
    if isinstance(existing, list):
        offset = len(initial_attempt_history)
        for item in existing:
            if isinstance(item, dict):
                copied = dict(item)
                attempt = copied.get("attempt")
                if isinstance(attempt, int) and not isinstance(attempt, bool):
                    copied["attempt"] = attempt + offset
                merged.append(copied)
            else:
                merged.append(item)
    metadata["attempt_history"] = merged
    return replace(result, metadata=metadata)


def _run_in_sandbox(
    *,
    sandbox: GitSandbox,
    task: DelegationTask,
    worker: Any,
    context: str,
    first_response: Any,
    started: float,
    context_chars: int,
    attempt_offset: int = 0,
    retry_limit_override: int | None = None,
) -> DelegationResult:
    response = first_response
    retry: RetryEvidence | None = None
    attempt_history: list[dict[str, Any]] = []
    last_verification: tuple[VerificationResult, ...] = ()
    last_patch = ""
    last_files: tuple[str, ...] = ()

    def record_attempt(response_value: Any, *, status: str) -> None:
        attempt_history.append({
            "attempt": len(attempt_history) + 1,
            "status": status,
            "verification": [item.to_dict() for item in last_verification],
            "files_changed": list(last_files),
            "patch_bytes": len(last_patch.encode("utf-8")),
            "local_runtime": dict(getattr(response_value, "runtime", {}) or {}),
        })

    retry_limit = task.retry_limit if retry_limit_override is None else retry_limit_override
    for attempt in range(1, retry_limit + 2):
        total_attempt = attempt_offset + attempt
        sandbox.reset()
        last_verification = ()
        last_patch = ""
        last_files = ()
        try:
            if response.blocked:
                record_attempt(response, status="BLOCKED")
                return _result(
                    task=task,
                    status=ResultStatus.BLOCKED,
                    started=started,
                    summary=response.summary,
                    blockers=response.blockers,
                    attempts=total_attempt,
                    sandbox_mode=sandbox.mode,
                metadata={
                    "context_chars": context_chars,
                    "worker_runtime": dict(getattr(response, "runtime", {}) or {}),
                    "attempt_history": attempt_history,
                },
            )
            candidate_patch = _coerce_worker_patch(response, sandbox.path, task)
            validate_patch_scope(candidate_patch, task.allowed_files)
            if not task.verification:
                record_attempt(response, status="WORKER_ERROR")
                return _result(
                    task=task,
                    status=ResultStatus.WORKER_ERROR,
                    started=started,
                    summary="Delegation requires at least one verification command.",
                    blockers=("task contract did not declare verification commands",),
                    attempts=total_attempt,
                    sandbox_mode=sandbox.mode,
                    metadata={
                        "context_chars": context_chars,
                        "attempt_history": attempt_history,
                    },
                )
            apply_patch(sandbox.path, candidate_patch)
            verification = run_verification(
                task.verification,
                sandbox.path,
            )
            last_verification = verification
            sandbox.clean_verification_artifacts()
            last_patch = capture_diff(sandbox.path)
            last_files = changed_files(sandbox.path)
            if last_patch.strip():
                validate_patch_scope(last_patch, task.allowed_files)
        except ScopeViolationError as exc:
            record_attempt(response, status="SCOPE_VIOLATION")
            return _result(
                task=task,
                status=ResultStatus.SCOPE_VIOLATION,
                started=started,
                summary="Worker patch violated the allowed file scope.",
                files_changed=last_files,
                patch=last_patch,
                verification=last_verification,
                blockers=(str(exc),),
                attempts=total_attempt,
                sandbox_mode=sandbox.mode,
                metadata={"context_chars": context_chars, "attempt_history": attempt_history},
            )
        except PatchError as exc:
            record_attempt(response, status="WORKER_ERROR")
            if attempt <= retry_limit:
                retry = RetryEvidence(
                    previous_patch=_response_candidate(response),
                    verification=(),
                    failure_summary=str(exc)[:2000],
                )
                try:
                    response = worker.run(task, context, retry)
                except Exception as retry_exc:
                    attempt_history.append({
                        "attempt": total_attempt + 1,
                        "status": "WORKER_ERROR",
                        "verification": [],
                        "files_changed": [],
                        "patch_bytes": 0,
                        "local_runtime": dict(
                            getattr(retry_exc, "runtime", {}) or {}
                        ),
                    })
                    return _result(
                        task=task,
                        status=ResultStatus.WORKER_ERROR,
                        started=started,
                        summary="Worker retry failed before producing a replacement patch.",
                        files_changed=last_files,
                        patch=last_patch,
                        verification=last_verification,
                        blockers=(str(retry_exc),),
                        attempts=total_attempt + 1,
                        sandbox_mode=sandbox.mode,
                        metadata={
                            "context_chars": context_chars,
                            "attempt_history": attempt_history,
                        },
                    )
                continue
            return _result(
                task=task,
                status=ResultStatus.WORKER_ERROR,
                started=started,
                summary="Worker produced a patch that could not be applied safely.",
                files_changed=last_files,
                patch=last_patch,
                verification=last_verification,
                blockers=(str(exc),),
                attempts=total_attempt,
                sandbox_mode=sandbox.mode,
                metadata={"context_chars": context_chars, "attempt_history": attempt_history},
            )

        passed = all(item.passed for item in last_verification)
        attempt_history.append({
            "attempt": attempt,
            "status": "VERIFIED" if all(item.passed for item in last_verification) else "FAILED_VERIFICATION",
            "verification": [item.to_dict() for item in last_verification],
            "files_changed": list(last_files),
            "patch_bytes": len(last_patch.encode("utf-8")),
            "local_runtime": dict(getattr(response, "runtime", {}) or {}),
        })
        if passed:
            return _result(
                task=task,
                status=ResultStatus.SUCCESS,
                started=started,
                summary=response.summary or "Delegated patch passed verification.",
                files_changed=last_files,
                patch=last_patch,
                verification=last_verification,
                blockers=response.blockers,
                attempts=total_attempt,
                sandbox_mode=sandbox.mode,
                metadata={
                    "context_chars": context_chars,
                    "attempt_history": attempt_history,
                },
            )

        failure_summary, verification_dicts = _failure_summary(last_verification)
        if attempt > retry_limit:
            return _result(
                task=task,
                status=ResultStatus.FAILED_VERIFICATION,
                started=started,
                summary=response.summary or "Delegated patch failed verification.",
                files_changed=last_files,
                patch=last_patch,
                verification=last_verification,
                blockers=(failure_summary,),
                attempts=total_attempt,
                sandbox_mode=sandbox.mode,
                metadata={
                    "context_chars": context_chars,
                    "attempt_history": attempt_history,
                },
            )

        retry = RetryEvidence(
            previous_patch=last_patch,
            verification=tuple(verification_dicts),
            failure_summary=failure_summary,
        )
        try:
            response = worker.run(task, context, retry)
        except Exception as exc:
            attempt_history.append({
                "attempt": total_attempt + 1,
                "status": "WORKER_ERROR",
                "verification": [],
                "files_changed": [],
                "patch_bytes": 0,
                "local_runtime": dict(getattr(exc, "runtime", {}) or {}),
            })
            return _result(
                task=task,
                status=ResultStatus.WORKER_ERROR,
                started=started,
                summary="Worker retry failed before producing a replacement patch.",
                files_changed=last_files,
                patch=last_patch,
                verification=last_verification,
                blockers=(str(exc),),
                attempts=total_attempt + 1,
                sandbox_mode=sandbox.mode,
                metadata={
                    "context_chars": context_chars,
                    "attempt_history": attempt_history,
                },
            )

    raise DelegationError("delegation loop exited without a result")


def delegate_local(
    *,
    objective: str | None = None,
    allowed_files: Sequence[str] | None = None,
    context: Sequence[str] | None = None,
    requirements: Sequence[str] | None = None,
    constraints: Sequence[str] | None = None,
    verification: Sequence[str] | None = None,
    success_criteria: Sequence[str] | None = None,
    context_mode: str = "replace",
    task_kind: str = "unspecified",
    risk_flags: Sequence[str] | None = None,
    repo: str | Path = ".",
    model: str | None = None,
    task: DelegationTask | None = None,
    client: OllamaClient | None = None,
    worker: Any | None = None,
) -> DelegationResult:
    started = time.perf_counter()
    if task is not None and objective is not None:
        raise TaskContractError("pass either task= or objective=, not both")
    if task is not None and (task_kind != "unspecified" or risk_flags):
        raise TaskContractError(
            "pass task_kind and risk_flags inside task=DelegationTask"
        )
    if task is None:
        task = DelegationTask(
            task_id=f"ar-{int(time.time() * 1000)}",
            objective=objective or "",
            allowed_files=tuple(allowed_files or ()),
            context=tuple(context or ()),
            requirements=tuple(requirements or ()),
            constraints=tuple(constraints or ()),
            verification=tuple(verification or ()),
            success_criteria=tuple(success_criteria or ()),
            model=model,
            context_mode=context_mode,
            task_kind=task_kind,
            risk_flags=tuple(risk_flags or ()),
        )
    elif model is not None and task.model != model:
        task = replace(task, model=model)

    root = Path(repo).resolve()
    initial_snapshot = _safe_snapshot(root)
    if initial_snapshot is None:
        return _result(
            task=task,
            status=ResultStatus.WORKER_ERROR,
            started=started,
            summary="Could not inspect the main worktree before delegation.",
            blockers=("git status failed before delegation",),
        )
    try:
        context_text = collect_context(root, task)
    except Exception as exc:
        return _result(
            task=task,
            status=ResultStatus.WORKER_ERROR,
            started=started,
            summary="Could not gather bounded task context.",
            blockers=(str(exc),),
        )

    selected_worker = worker
    if selected_worker is None:
        selected_client = client or OllamaClient()
        selected_worker = OllamaWorker(selected_client, model or task.model)

    first_response = None
    initial_attempts = 0
    initial_failures: list[str] = []
    initial_attempt_history: list[dict[str, Any]] = []
    initial_worker_runtime: dict[str, Any] = {}
    initial_retry: RetryEvidence | None = None
    for initial_attempt in range(1, task.retry_limit + 2):
        initial_attempts = initial_attempt
        try:
            first_response = selected_worker.run(task, context_text, initial_retry)
            break
        except Exception as exc:
            failure_detail = _worker_failure_detail(exc)
            initial_failures.append(failure_detail)
            runtime = getattr(exc, "runtime", {}) or {}
            initial_attempt_history.append({
                "attempt": initial_attempt,
                "status": (
                    "TIMEOUT"
                    if (
                        isinstance(exc, CodexCliError)
                        and exc.timed_out
                    ) or "timed out" in str(exc).lower()
                    else "WORKER_ERROR"
                ),
                "verification": [],
                "files_changed": [],
                "patch_bytes": 0,
                "failure": failure_detail[:2000],
                "local_runtime": dict(runtime) if isinstance(runtime, dict) else {},
            })
            if isinstance(exc, CodexCliError) and exc.runtime:
                initial_worker_runtime = dict(exc.runtime)
            if initial_attempt <= task.retry_limit and not (
                isinstance(exc, CodexCliError) and not exc.retryable
            ):
                initial_retry = RetryEvidence(
                    previous_patch="",
                    verification=(),
                    failure_summary=failure_detail,
                )
                continue
            if isinstance(exc, (OllamaError, CodexCliError)):
                status = (
                    ResultStatus.TIMEOUT
                    if (
                        isinstance(exc, CodexCliError)
                        and exc.timed_out
                    ) or "timed out" in str(exc).lower()
                    else ResultStatus.WORKER_ERROR
                )
                summary = (
                    "Codex CLI local-model worker request failed."
                    if isinstance(exc, CodexCliError)
                    else "Ollama worker request failed."
                )
            else:
                status = ResultStatus.WORKER_ERROR
                summary = "Worker response could not be produced or parsed."
            result = _result(
                task=task,
                status=status,
                started=started,
                summary=summary,
                blockers=(str(exc),),
                attempts=initial_attempt,
                metadata={
                    "context_chars": len(context_text),
                    "initial_worker_failures": initial_failures,
                    "worker_runtime": initial_worker_runtime,
                    "attempt_history": initial_attempt_history,
                },
            )
            return _check_main_worktree(root, initial_snapshot, result)

    if first_response is None:
        raise DelegationError("initial worker loop exited without a response")

    if first_response.blocked:
        blocked_attempt = {
            "attempt": initial_attempts,
            "status": "BLOCKED",
            "verification": [],
            "files_changed": [],
            "patch_bytes": 0,
            "local_runtime": dict(getattr(first_response, "runtime", {}) or {}),
        }
        result = _result(
            task=task,
            status=ResultStatus.BLOCKED,
            started=started,
            summary=first_response.summary,
            blockers=first_response.blockers,
            attempts=initial_attempts,
            metadata={
                "context_chars": len(context_text),
                "initial_worker_failures": initial_failures,
                "worker_runtime": dict(getattr(first_response, "runtime", {}) or {}),
                "attempt_history": initial_attempt_history + [blocked_attempt],
            },
        )
        return _check_main_worktree(root, initial_snapshot, result)

    try:
        with GitSandbox(root, task.task_id) as sandbox:
            result = _run_in_sandbox(
                sandbox=sandbox,
                task=task,
                worker=selected_worker,
                context=context_text,
                first_response=first_response,
                started=started,
                context_chars=len(context_text),
                attempt_offset=initial_attempts - 1,
                retry_limit_override=max(0, task.retry_limit - (initial_attempts - 1)),
            )
    except SandboxError as exc:
        result = _result(
            task=task,
            status=ResultStatus.WORKER_ERROR,
            started=started,
            summary="Could not create an isolated sandbox.",
            blockers=(str(exc),),
            metadata={"context_chars": len(context_text)},
        )
    except Exception as exc:
        result = _result(
            task=task,
            status=ResultStatus.WORKER_ERROR,
            started=started,
            summary="Delegation orchestration failed.",
            blockers=(f"{type(exc).__name__}: {exc}",),
            metadata={"context_chars": len(context_text)},
        )
    result = _merge_initial_attempt_history(result, initial_attempt_history)
    result = _check_main_worktree(root, initial_snapshot, result)
    if initial_failures:
        result.metadata["initial_worker_failures"] = initial_failures
    return result


def _check_main_worktree(
    root: Path,
    initial_snapshot: tuple[str, ...] | None,
    result: DelegationResult,
) -> DelegationResult:
    current_snapshot = _safe_snapshot(root)
    if initial_snapshot is None or current_snapshot is None:
        result.metadata["main_worktree_unchanged"] = False
        return replace(
            result,
            status=ResultStatus.WORKER_ERROR,
            summary="Could not verify that the main worktree remained unchanged.",
            blockers=tuple(result.blockers) + (
                "git status failed while checking main worktree integrity",
            ),
        )
    result.metadata["main_worktree_unchanged"] = current_snapshot == initial_snapshot
    result.metadata["initial_worktree_status"] = list(initial_snapshot)
    result.metadata["final_worktree_status"] = list(current_snapshot)
    if current_snapshot == initial_snapshot:
        return result
    blockers = tuple(result.blockers) + (
        "main worktree changed during delegation; result rejected",
    )
    return replace(
        result,
        status=ResultStatus.SCOPE_VIOLATION,
        summary="Main worktree changed during delegation.",
        blockers=blockers,
    )
