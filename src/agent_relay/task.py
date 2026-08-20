from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import Any, Mapping, Sequence


class TaskContractError(ValueError):
    """Raised when a delegation task violates the bounded contract."""


_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_RANGE_SUFFIX = re.compile(r"^(?P<path>.+?)(?::(?P<start>[0-9]+)(?:-(?P<end>[0-9]+))?)?$")

# ``task_kind`` is intentionally small.  It is a routing signal for the
# frontier agent, not a second taxonomy of all software work.  The triage
# layer treats only the low-risk kinds as delegation candidates.
TASK_KINDS = frozenset({
    "unspecified",
    "mechanical",
    "test_generation",
    "repetitive",
    "bounded_bugfix",
    "documentation",
    "formatting",
    "architecture",
    "security",
    "debugging",
    "migration",
    "product_decision",
    "ambiguous",
    "dependency_update",
    "performance",
})


def normalize_relative_path(value: str) -> str:
    if not isinstance(value, str):
        raise TaskContractError("paths must be strings")
    raw = value.strip().replace("\\", "/")
    if not raw:
        raise TaskContractError("paths must not be empty")
    if raw.startswith("/") or _WINDOWS_DRIVE.match(raw):
        raise TaskContractError(f"path must be repository-relative: {value!r}")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise TaskContractError(f"path contains an invalid segment: {value!r}")
    normalized = str(PurePosixPath(*parts))
    if normalized in {".", ""} or normalized.startswith("../"):
        raise TaskContractError(f"path must be repository-relative: {value!r}")
    return normalized


def _tuple_of_strings(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Sequence):
        raise TaskContractError(f"{field_name} must be a list of strings")
    values = tuple(item.strip() if isinstance(item, str) else item for item in value)
    if any(not isinstance(item, str) or not item for item in values):
        raise TaskContractError(f"{field_name} must contain non-empty strings")
    return values


def context_path_and_range(spec: str) -> tuple[str, int | None, int | None]:
    match = _RANGE_SUFFIX.match(spec.strip())
    if not match:
        raise TaskContractError(f"invalid context spec: {spec!r}")
    path = normalize_relative_path(match.group("path"))
    start = int(match.group("start")) if match.group("start") else None
    end = int(match.group("end")) if match.group("end") else start
    if start is not None and start < 1:
        raise TaskContractError(f"context range must start at line 1 or later: {spec!r}")
    if end is not None and end < 1:
        raise TaskContractError(f"context range must end at line 1 or later: {spec!r}")
    if start is not None and end is not None and end < start:
        raise TaskContractError(f"context range ends before it starts: {spec!r}")
    return path, start, end


@dataclass(frozen=True)
class DelegationTask:
    task_id: str
    objective: str
    allowed_files: tuple[str, ...]
    context: tuple[str, ...] = ()
    requirements: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    verification: tuple[str, ...] = ()
    success_criteria: tuple[str, ...] = ()
    model: str | None = None
    retry_limit: int = 1
    context_mode: str = "replace"
    task_kind: str = "unspecified"
    risk_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise TaskContractError("task_id must be a non-empty string")
        if not isinstance(self.objective, str) or not self.objective.strip():
            raise TaskContractError("objective must be a non-empty string")

        normalized_files = tuple(normalize_relative_path(path) for path in self.allowed_files)
        if len(set(normalized_files)) != len(normalized_files):
            raise TaskContractError("allowed_files must not contain duplicates")
        object.__setattr__(self, "allowed_files", normalized_files)

        for field_name in ("context", "requirements", "constraints", "verification", "success_criteria"):
            object.__setattr__(
                self,
                field_name,
                _tuple_of_strings(getattr(self, field_name), field_name),
            )

        if not isinstance(self.task_kind, str) or not self.task_kind.strip():
            raise TaskContractError("task_kind must be a non-empty string")
        normalized_kind = self.task_kind.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized_kind not in TASK_KINDS:
            allowed = ", ".join(sorted(TASK_KINDS))
            raise TaskContractError(
                f"task_kind must be one of {allowed}"
            )
        object.__setattr__(self, "task_kind", normalized_kind)
        normalized_risk_flags = tuple(
            item.strip().lower().replace("-", "_").replace(" ", "_")
            for item in _tuple_of_strings(self.risk_flags, "risk_flags")
        )
        object.__setattr__(self, "risk_flags", normalized_risk_flags)

        for spec in self.context:
            # Context is read-only input. It may include tests or neighboring
            # files that explain the contract, while allowed_files remains the
            # write boundary enforced on every returned patch.
            context_path_and_range(spec)

        if self.model is not None and (not isinstance(self.model, str) or not self.model.strip()):
            raise TaskContractError("model must be a non-empty string when provided")
        if not isinstance(self.retry_limit, int) or isinstance(self.retry_limit, bool):
            raise TaskContractError("retry_limit must be an integer")
        if self.retry_limit not in (0, 1):
            raise TaskContractError("retry_limit must be 0 or 1")
        if not isinstance(self.context_mode, str) or self.context_mode not in {"replace", "insert_after"}:
            raise TaskContractError("context_mode must be replace or insert_after")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DelegationTask:
        if not isinstance(value, Mapping):
            raise TaskContractError("task must be a JSON object")
        return cls(
            task_id=value.get("task_id", ""),
            objective=value.get("objective", ""),
            allowed_files=value.get("allowed_files", ()),
            context=value.get("context", ()),
            requirements=value.get("requirements", ()),
            constraints=value.get("constraints", ()),
            verification=value.get("verification", ()),
            success_criteria=value.get("success_criteria", ()),
            model=value.get("model"),
            retry_limit=value.get("retry_limit", 1),
            context_mode=value.get("context_mode", "replace"),
            task_kind=value.get("task_kind", "unspecified"),
            risk_flags=value.get("risk_flags", ()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "objective": self.objective,
            "allowed_files": list(self.allowed_files),
            "context": list(self.context),
            "requirements": list(self.requirements),
            "constraints": list(self.constraints),
            "verification": list(self.verification),
            "success_criteria": list(self.success_criteria),
            "model": self.model,
            "retry_limit": self.retry_limit,
            "context_mode": self.context_mode,
            "task_kind": self.task_kind,
            "risk_flags": list(self.risk_flags),
        }
