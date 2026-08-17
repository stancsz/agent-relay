from __future__ import annotations

from pathlib import Path
import subprocess
import time
from typing import Iterable

from .result import VerificationResult


def _bounded(value: str, limit: int = 20000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[output truncated]"


def run_verification(
    commands: Iterable[str],
    cwd: str | Path,
    *,
    timeout_seconds: float = 120.0,
) -> tuple[VerificationResult, ...]:
    results: list[VerificationResult] = []
    for command in commands:
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                command,
                cwd=Path(cwd),
                shell=True,
                text=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = time.perf_counter() - started
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            results.append(
                VerificationResult(
                    command=command,
                    exit_code=None,
                    stdout=_bounded(stdout),
                    stderr=_bounded(
                        stderr
                        + f"\ncommand timed out after {timeout_seconds:g} seconds"
                    ),
                    duration_seconds=elapsed,
                    timed_out=True,
                )
            )
            continue

        results.append(
            VerificationResult(
                command=command,
                exit_code=completed.returncode,
                stdout=_bounded(completed.stdout),
                stderr=_bounded(completed.stderr),
                duration_seconds=time.perf_counter() - started,
            )
        )
    return tuple(results)

