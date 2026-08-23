from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from typing import Any, Mapping


class ResultStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED_VERIFICATION = "FAILED_VERIFICATION"
    SCOPE_VIOLATION = "SCOPE_VIOLATION"
    BLOCKED = "BLOCKED"
    WORKER_ERROR = "WORKER_ERROR"
    TIMEOUT = "TIMEOUT"


RECEIPT_PROTOCOL = "agent-relay/0.2"


@dataclass(frozen=True)
class VerificationResult:
    command: str
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_seconds": self.duration_seconds,
            "timed_out": self.timed_out,
            "passed": self.passed,
        }


@dataclass(frozen=True)
class WorkerResponse:
    status: str
    summary: str = ""
    patch: str = ""
    blockers: tuple[str, ...] = ()
    file_contents: tuple[tuple[str, str], ...] = ()
    runtime: Mapping[str, Any] = field(default_factory=dict)
    raw_response: str = ""

    @property
    def blocked(self) -> bool:
        return self.status.upper() == "BLOCKED"

    def to_dict(self, include_raw: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "status": self.status,
            "summary": self.summary,
            "patch": self.patch,
            "blockers": list(self.blockers),
            "files": {path: content for path, content in self.file_contents},
            "runtime": dict(self.runtime),
        }
        if include_raw:
            value["raw_response"] = self.raw_response
        return value


@dataclass(frozen=True)
class DelegationResult:
    task_id: str
    status: ResultStatus
    summary: str = ""
    files_changed: tuple[str, ...] = ()
    patch: str = ""
    verification: tuple[VerificationResult, ...] = ()
    blockers: tuple[str, ...] = ()
    attempts: int = 0
    duration_seconds: float = 0.0
    sandbox_mode: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.status is ResultStatus.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "protocol": RECEIPT_PROTOCOL,
            "task_id": self.task_id,
            "status": self.status.value,
            "summary": self.summary,
            "files_changed": list(self.files_changed),
            "patch": self.patch,
            "verification": [item.to_dict() for item in self.verification],
            "blockers": list(self.blockers),
            "attempts": self.attempts,
            "duration_seconds": self.duration_seconds,
            "sandbox_mode": self.sandbox_mode,
            "metadata": self.metadata,
        }
        lane = self.metadata.get("lane")
        if isinstance(lane, str) and lane:
            payload["lane"] = lane
        return payload

    def to_handoff(self, *, patch_artifact: str | None = None) -> dict[str, Any]:
        """Return a compact frontier-facing proof packet without patch text.

        The full patch remains available to the caller through ``patch`` or an
        explicitly written artifact. Keeping it out of the normal handoff is
        the important token-saving path: Codex receives status, scope, and
        verification evidence first, and opens the artifact only when review
        or integration requires it.
        """

        patch_bytes = len(self.patch.encode("utf-8"))
        verification: list[dict[str, Any]] = []
        for item in self.verification:
            value: dict[str, Any] = {
                "command": item.command,
                "exit_code": item.exit_code,
                "passed": item.passed,
            }
            if item.timed_out:
                value["timed_out"] = True
            if not item.passed:
                value["failure_tail"] = (item.stderr or item.stdout)[-240:]
            verification.append(value)
        attempt_history = self.metadata.get("attempt_history", [])
        last_runtime: Mapping[str, Any] = {}
        if isinstance(attempt_history, list) and attempt_history:
            candidate = attempt_history[-1]
            if isinstance(candidate, Mapping):
                runtime = candidate.get("local_runtime")
                if isinstance(runtime, Mapping):
                    last_runtime = runtime
        if not last_runtime:
            runtime = self.metadata.get("worker_runtime")
            if isinstance(runtime, Mapping):
                last_runtime = runtime
        handoff: dict[str, Any] = {
            "protocol": RECEIPT_PROTOCOL,
            "task_id": self.task_id,
            "status": self.status.value,
            "summary": self.summary[:160],
            "files_changed": list(self.files_changed),
            "verification": verification,
            "patch": {
                "sha256": hashlib.sha256(self.patch.encode("utf-8")).hexdigest(),
                "bytes": patch_bytes,
            },
        }
        lane = self.metadata.get("lane")
        if isinstance(lane, str) and lane:
            handoff["lane"] = lane
        if self.attempts != 1:
            handoff["attempts"] = self.attempts
        if self.blockers:
            handoff["blockers"] = list(self.blockers)[:3]
        if self.metadata.get("main_worktree_unchanged") is not True:
            handoff["main_worktree_unchanged"] = self.metadata.get(
                "main_worktree_unchanged"
            )
        if last_runtime.get("result_source"):
            handoff["result_source"] = last_runtime["result_source"]
        if patch_artifact is not None:
            handoff["patch"]["artifact"] = patch_artifact
        # Useful for measuring the frontier-facing packet without pretending
        # that local-model tokens are Codex tokens.
        handoff["handoff_tokens_estimate"] = max(
            1,
            (len(json.dumps(handoff, ensure_ascii=False, separators=(",", ":"))) + 3)
            // 4,
        )
        return handoff
