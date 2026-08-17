from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import tempfile
from typing import Sequence


class SandboxError(RuntimeError):
    """Raised when an isolated execution workspace cannot be created."""


def _git(repo: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


class GitSandbox:
    """Create a disposable Git worktree, with a safe copy fallback for dirty repos."""

    def __init__(self, source_repo: str | Path, task_id: str) -> None:
        self.source_repo = Path(source_repo).resolve()
        self.task_id = task_id
        self.root: Path | None = None
        self.path: Path | None = None
        self.mode: str | None = None

    def __enter__(self) -> GitSandbox:
        if not self.source_repo.is_dir():
            raise SandboxError(f"repository path is not a directory: {self.source_repo}")
        self.root = Path(tempfile.mkdtemp(prefix="lcd-sandbox-"))
        clean_git = self._has_clean_commit()
        if clean_git:
            self.mode = "worktree"
            self.path = self.root / "worktree"
            result = _git(
                self.source_repo,
                ["worktree", "add", "--detach", str(self.path), "HEAD"],
            )
            if result.returncode != 0:
                self._remove_root()
                raise SandboxError(
                    f"git worktree add failed: {(result.stderr or result.stdout).strip()[:1000]}"
                )
        else:
            self.mode = "copy"
            self.path = self.root / "copy"
            self._create_copy()
        return self

    def _has_clean_commit(self) -> bool:
        head = _git(self.source_repo, ["rev-parse", "--verify", "HEAD"])
        if head.returncode != 0:
            return False
        status = _git(
            self.source_repo,
            ["status", "--porcelain", "--untracked-files=all"],
        )
        return status.returncode == 0 and not status.stdout.strip()

    def _create_copy(self) -> None:
        assert self.path is not None
        try:
            shutil.copytree(
                self.source_repo,
                self.path,
                ignore=shutil.ignore_patterns(
                    ".git",
                    "__pycache__",
                    ".pytest_cache",
                    ".mypy_cache",
                    ".ruff_cache",
                ),
            )
        except OSError as exc:
            raise SandboxError(f"could not copy repository into sandbox: {exc}") from exc

        for args in (
            ["init"],
            ["config", "user.name", "Local Code Delegate"],
            ["config", "user.email", "local-code-delegate@example.invalid"],
            ["add", "-A"],
            ["commit", "-m", "sandbox baseline", "--quiet"],
        ):
            result = _git(self.path, args)
            if result.returncode != 0:
                raise SandboxError(
                    f"could not initialize sandbox Git repository: "
                    f"{(result.stderr or result.stdout).strip()[:1000]}"
                )

    def reset(self) -> None:
        if self.path is None:
            raise SandboxError("sandbox is not open")
        result = _git(self.path, ["reset", "--hard", "HEAD"])
        if result.returncode != 0:
            raise SandboxError(f"sandbox reset failed: {result.stderr.strip()[:500]}")
        result = _git(self.path, ["clean", "-fdx"])
        if result.returncode != 0:
            raise SandboxError(f"sandbox cleanup failed: {result.stderr.strip()[:500]}")

    def clean_verification_artifacts(self) -> None:
        """Remove predictable test-tool artifacts before collecting the patch."""
        if self.path is None:
            raise SandboxError("sandbox is not open")
        generated_dirs = {
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
        }
        for current, directories, files in os.walk(self.path):
            current_path = Path(current)
            for name in list(directories):
                if name in generated_dirs:
                    shutil.rmtree(current_path / name, ignore_errors=True)
                    directories.remove(name)
            for name in files:
                if name.endswith(".pyc") or name in {".coverage"}:
                    (current_path / name).unlink(missing_ok=True)

    def _remove_root(self) -> None:
        if self.root is not None:
            shutil.rmtree(self.root, ignore_errors=True)
            self.root = None
            self.path = None

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.mode == "worktree" and self.path is not None:
            result = _git(
                self.source_repo,
                ["worktree", "remove", "--force", str(self.path)],
            )
            if result.returncode != 0:
                shutil.rmtree(self.path, ignore_errors=True)
        self._remove_root()
        self.mode = None
