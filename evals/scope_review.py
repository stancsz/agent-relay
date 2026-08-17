"""Conservative task-aware review for bounded evaluation patches.

Path containment is necessary but not sufficient: a worker can make an
unrelated edit inside an allowed file.  This module checks the changed hunk
locations against the task's declared context and treats uncertainty as an
incomplete review rather than silently declaring the patch safe.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from local_code_delegate.patch import patch_paths, validate_patch_scope
from local_code_delegate.task import (
    DelegationTask,
    context_path_and_range,
    normalize_relative_path,
)


_HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)
_INSERT_ANCHOR_TOLERANCE = 3


@dataclass(frozen=True)
class _Hunk:
    path: str
    old_start: int
    old_count: int
    old_lines: tuple[str, ...]
    changed_old_points: tuple[int, ...]
    deleted_lines: tuple[str, ...]
    deleted_old_points: tuple[int, ...]


def _marker_path(value: str) -> str | None:
    token = value.strip().split("\t", 1)[0].strip()
    if token == "/dev/null":
        return None
    if token.startswith(("a/", "b/")):
        token = token[2:]
    return normalize_relative_path(token)


def _parse_hunks(patch: str) -> tuple[_Hunk, ...]:
    """Parse enough unified-diff structure to locate changed old-side lines."""

    hunks: list[_Hunk] = []
    current_path: str | None = None
    lines = patch.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("diff --git "):
            current_path = None
        elif line.startswith("+++ "):
            current_path = _marker_path(line[4:])
        elif line.startswith("@@ "):
            match = _HUNK_HEADER.match(line)
            if match is None or current_path is None:
                raise ValueError("malformed unified-diff hunk")
            old_start = int(match.group(1))
            old_count = int(match.group(2) or "1")
            body: list[str] = []
            index += 1
            while index < len(lines):
                candidate = lines[index]
                if candidate.startswith(("@@ ", "diff --git ")):
                    break
                body.append(candidate)
                index += 1
            old_lines: list[str] = []
            changed_old_points: list[int] = []
            deleted_lines: list[str] = []
            deleted_old_points: list[int] = []
            old_cursor = old_start
            for body_line in body:
                if not body_line:
                    raise ValueError("malformed empty unified-diff body line")
                marker = body_line[0]
                content = body_line[1:]
                if marker == " ":
                    old_lines.append(content)
                    old_cursor += 1
                elif marker == "-":
                    old_lines.append(content)
                    deleted_lines.append(content)
                    changed_old_points.append(old_cursor)
                    deleted_old_points.append(old_cursor)
                    old_cursor += 1
                elif marker == "+":
                    changed_old_points.append(old_cursor)
                elif marker == "\\":
                    continue
                else:
                    raise ValueError(f"malformed unified-diff body line: {body_line!r}")
            hunks.append(
                _Hunk(
                    path=current_path,
                    old_start=old_start,
                    old_count=old_count,
                    old_lines=tuple(old_lines),
                    changed_old_points=tuple(changed_old_points),
                    deleted_lines=tuple(deleted_lines),
                    deleted_old_points=tuple(deleted_old_points),
                )
            )
            continue
        index += 1
    if not hunks:
        raise ValueError("patch contained no unified-diff hunks")
    return tuple(hunks)


def _context_ranges(task: DelegationTask) -> dict[str, tuple[tuple[int, int], ...]]:
    ranges: dict[str, list[tuple[int, int]]] = {}
    for spec in task.context:
        path, start, end = context_path_and_range(spec)
        if start is None or end is None:
            continue
        ranges.setdefault(path, []).append((start, end))
    return {path: tuple(values) for path, values in ranges.items()}


def _read_context_lines(
    repository: Path,
    path: str,
    start: int,
    end: int,
) -> tuple[str, ...] | None:
    target = repository / Path(*path.split("/"))
    try:
        target.resolve(strict=True).relative_to(repository.resolve())
    except (OSError, ValueError):
        return None
    if not target.is_file() or target.is_symlink():
        return None
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    if start < 1 or end > len(lines):
        return None
    return tuple(lines[start - 1 : end])


def _contains_contiguous(values: Sequence[str], needle: Sequence[str]) -> bool:
    if not needle or len(needle) > len(values):
        return False
    width = len(needle)
    return any(
        tuple(values[index : index + width]) == tuple(needle)
        for index in range(len(values) - width + 1)
    )


def review_task_patch(
    patch: str,
    task: DelegationTask,
    *,
    repository: str | Path,
    expected_files: Sequence[str] = (),
    expected_patch: str | None = None,
) -> dict[str, Any]:
    """Review path and task-context containment without making semantic claims.

    ``reviewed`` means the automated path/context checks completed.  ``violation``
    means evidence shows the patch crossed the declared write or context
    boundary.  A missing patch or malformed diff is instead incomplete, so a
    caller cannot convert a failed worker into a clean scope result.  Git's
    diff algorithm can move identical blank context lines around an
    ``insert_after`` hunk; when an oracle patch is available, a small bounded
    anchor tolerance handles that representation detail without accepting a
    distant insertion.
    """

    reasons: list[str] = []
    if not isinstance(patch, str) or not patch.strip():
        return {
            "reviewed": False,
            "violation": False,
            "basis": "patch unavailable; path/context review incomplete",
            "reasons": ["patch is empty"],
            "paths": [],
            "hunks": 0,
        }

    try:
        paths = tuple(patch_paths(patch))
        validate_patch_scope(patch, task.allowed_files)
        hunks = _parse_hunks(patch)
    except Exception as exc:
        return {
            "reviewed": False,
            "violation": True,
            "basis": "automated patch parsing failed",
            "reasons": [str(exc)[:500]],
            "paths": [],
            "hunks": 0,
        }

    expected_hunks: tuple[_Hunk, ...] = ()
    if isinstance(expected_patch, str) and expected_patch.strip():
        try:
            expected_hunks = _parse_hunks(expected_patch)
        except ValueError:
            expected_hunks = ()

    expected = tuple(sorted(normalize_relative_path(item) for item in expected_files))
    actual = tuple(sorted(set(paths)))
    if expected and actual != expected:
        reasons.append(f"changed paths {list(actual)} do not match expected files {list(expected)}")

    ranges = _context_ranges(task)
    if not ranges:
        return {
            "reviewed": False,
            "violation": False,
            "basis": "task has no line-ranged context; task-aware review incomplete",
            "reasons": ["task has no line-ranged context for a task-aware review"],
            "paths": list(actual),
            "hunks": len(hunks),
        }

    repository_path = Path(repository).resolve()
    for hunk in hunks:
        if not hunk.changed_old_points:
            continue
        path_ranges = ranges.get(hunk.path, ())
        if not path_ranges:
            reasons.append(f"hunk path {hunk.path!r} has no declared context range")
            continue
        if task.context_mode == "insert_after":
            matching_range = None
            expected_points = tuple(
                point
                for expected_hunk in expected_hunks
                if expected_hunk.path == hunk.path
                for point in expected_hunk.changed_old_points
            )
            for start, end in path_ranges:
                declared = _read_context_lines(repository_path, hunk.path, start, end)
                if declared and _contains_contiguous(hunk.old_lines, declared):
                    matching_range = (start, end, declared)
                    break
                # Git may omit the first line of a declared range when its
                # generated hunk starts at the final context line.  The exact
                # insertion point is still a useful anchor in that case.
                if (
                    not hunk.deleted_lines
                    and len(set(hunk.changed_old_points)) == 1
                    and hunk.old_start
                    <= hunk.changed_old_points[0]
                    <= hunk.old_start + hunk.old_count
                    and (
                        any(
                            abs(hunk.changed_old_points[0] - point)
                            <= _INSERT_ANCHOR_TOLERANCE
                            for point in expected_points
                        )
                        if expected_points
                        else hunk.changed_old_points[0] == end + 1
                    )
                ):
                    matching_range = (start, end, declared or ())
                    break
            if matching_range is None:
                reasons.append(
                    f"insert_after hunk for {hunk.path!r} is not anchored to its declared context"
                )
                continue
            start, end, declared = matching_range
            if hunk.deleted_lines:
                reasons.append("insert_after patch deletes existing lines")
            insertion_points = set(hunk.changed_old_points)
            expected_point = end + 1
            if expected_points:
                anchor_ok = len(insertion_points) == 1 and any(
                    abs(next(iter(insertion_points)) - point)
                    <= _INSERT_ANCHOR_TOLERANCE
                    for point in expected_points
                )
            else:
                anchor_ok = insertion_points == {expected_point}
            if not anchor_ok:
                reasons.append(
                    f"insert_after change point {sorted(insertion_points)} is not after {hunk.path}:{start}-{end}"
                )
        else:
            for point in hunk.changed_old_points:
                if not any(start <= point <= end + 1 for start, end in path_ranges):
                    reasons.append(
                        f"change point {hunk.path}:{point} is outside declared context"
                    )
                elif any(
                    point == end + 1 and point in hunk.deleted_old_points
                    for _, end in path_ranges
                ):
                    reasons.append(
                        f"deletion point {hunk.path}:{point} is after declared context"
                    )

    violation = bool(reasons)
    return {
        # ``reviewed`` means the checks completed. A completed review can
        # legitimately find a violation; keeping that distinction lets the
        # evaluator compute the violation rate instead of turning a detected
        # violation into an unevaluated denominator.
        "reviewed": True,
        "violation": violation,
        "basis": (
            "automated path containment + declared context-range review"
            if not violation
            else "automated path/context review found a boundary violation"
        ),
        "reasons": reasons[:8],
        "paths": list(actual),
        "hunks": len(hunks),
    }
