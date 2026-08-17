from pathlib import Path
import subprocess

import pytest

from local_code_delegate.patch import (
    ScopeViolationError,
    apply_patch,
    capture_diff,
    changed_files,
    normalize_patch_transport,
    rebase_unified_patch,
    validate_patch_scope,
)


def make_repo(path: Path) -> None:
    path.mkdir()
    (path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=path,
        check=True,
    )
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=path, check=True, capture_output=True)


def test_apply_capture_and_changed_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    patch = (
        "diff --git a/value.py b/value.py\n"
        "--- a/value.py\n"
        "+++ b/value.py\n"
        "@@ -1 +1 @@\n"
        "-VALUE = 1\n"
        "+VALUE = 2\n"
    )
    validate_patch_scope(patch, ["value.py"])
    apply_patch(repo, patch)
    assert changed_files(repo) == ("value.py",)
    assert "VALUE = 2" in capture_diff(repo)


def test_scope_violation_is_rejected() -> None:
    patch = (
        "diff --git a/other.py b/other.py\n"
        "--- a/other.py\n"
        "+++ b/other.py\n"
        "@@ -0,0 +1 @@\n"
        "+VALUE = 2\n"
    )
    with pytest.raises(ScopeViolationError):
        validate_patch_scope(patch, ["value.py"])


def test_normalize_patch_transport_drops_only_invalid_index_metadata(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    patch = (
        "diff --git a/value.py b/value.py\n"
        "index ... (old)\n"
        "--- a/value.py\n"
        "+++ b/value.py\n"
        "@@ -1 +1 @@\n"
        "-VALUE = 1\n"
        "+VALUE = 2\n"
    )

    normalized = normalize_patch_transport(patch)
    assert "index ... (old)" not in normalized
    apply_patch(repo, normalized)
    assert (repo / "value.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_normalize_patch_transport_preserves_valid_index_metadata() -> None:
    patch = "index 1234abcd..deadbeef 100644\n"

    assert normalize_patch_transport(patch) == patch


def test_normalize_patch_transport_decodes_one_line_escaped_diff() -> None:
    patch = (
        "diff --git a/value.py b/value.py/n"
        "--- a/value.py/n"
        "+++ b/value.py/n"
        "@@ -1 +1 @@/n"
        "-VALUE = 1/n"
        "+VALUE = 2/n"
    )

    normalized = normalize_patch_transport(patch)

    assert normalized.splitlines()[-2:] == ["-VALUE = 1", "+VALUE = 2"]


def test_rebase_unified_patch_matches_hunk_lines_without_newlines(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = "from typing import Any\n\n\ndef require_nonempty(value: str) -> str:\n    return value\n"
    (repo / "tasks.py").write_text(source, encoding="utf-8")
    patch = (
        "diff --git a/tasks.py b/tasks.py\n"
        "--- a/tasks.py\n"
        "+++ b/tasks.py\n"
        "@@ -1,3 +1,5 @@\n"
        " def require_nonempty(value: str) -> str:\n"
        "+    if not value:\n"
        "+        raise ValueError(\"value must not be empty\")\n"
        "     return value\n"
    )

    rebased = rebase_unified_patch(repo, patch)

    apply_patch(repo, rebased)
    assert "raise ValueError" in (repo / "tasks.py").read_text(encoding="utf-8")


def test_rebase_unified_patch_recovers_unique_deleted_line_with_stale_context(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = (
        "def first(value: int) -> int:\n"
        "    return value\n\n"
        "def second(value: int) -> int:\n"
        "    return value * 2\n"
    )
    (repo / "tasks.py").write_text(source, encoding="utf-8")
    patch = (
        "diff --git a/tasks.py b/tasks.py\n"
        "--- a/tasks.py\n"
        "+++ b/tasks.py\n"
        "@@ -99,1 +99,1 @@\n"
        " def stale_context() -> int:\n"
        "-    return value\n"
        "+    return value + 1\n"
    )

    rebased = rebase_unified_patch(repo, patch)

    apply_patch(repo, rebased)
    assert (repo / "tasks.py").read_text(encoding="utf-8") == (
        "def first(value: int) -> int:\n"
        "    return value + 1\n\n"
        "def second(value: int) -> int:\n"
        "    return value * 2\n"
    )
