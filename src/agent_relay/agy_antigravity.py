"""Google Antigravity CLI specialist lane.

Antigravity is exposed as a bounded specialist/advisor lane first.  It runs in
plan mode by default so Google-stack guidance, browser/UI reasoning, and
Firebase/Android ecosystem knowledge can be consulted without treating an AGY
self-report as an accepted patch.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Mapping

from .env import load_dotenv


DEFAULT_AGY_MODEL = "gemini-3.1-pro-high"
DEFAULT_AGY_EFFORT = "high"


@dataclass(frozen=True)
class AgyConfig:
    executable: str
    model: str = DEFAULT_AGY_MODEL
    effort: str = DEFAULT_AGY_EFFORT
    mode: str = "plan"
    sandbox: bool = True
    timeout_seconds: float = 300.0

    @classmethod
    def from_env(
        cls,
        *,
        executable: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        mode: str | None = None,
        sandbox: bool | None = None,
        timeout_seconds: float | None = None,
    ) -> "AgyConfig":
        load_dotenv()
        resolved = (
            executable
            or os.environ.get("AR_AGY_BIN")
            or shutil.which("agy.exe")
            or shutil.which("agy")
        )
        if not resolved:
            raise FileNotFoundError(
                "Antigravity CLI was not found; install agy or set AR_AGY_BIN"
            )
        raw_timeout = timeout_seconds
        if raw_timeout is None:
            try:
                raw_timeout = float(
                    os.environ.get("AR_AGY_TIMEOUT_SECONDS", "300")
                )
            except ValueError:
                raw_timeout = 300.0
        if raw_timeout <= 0:
            raise ValueError("AGY timeout must be greater than zero")
        selected_mode = mode or os.environ.get("AR_AGY_MODE", "plan")
        if selected_mode not in {"plan", "accept-edits"}:
            raise ValueError("AGY mode must be plan or accept-edits")
        selected_sandbox = sandbox
        if selected_sandbox is None:
            selected_sandbox = os.environ.get("AR_AGY_SANDBOX", "true").lower() in {
                "1", "true", "yes", "on"
            }
        return cls(
            executable=resolved,
            model=model or os.environ.get("AR_AGY_MODEL", DEFAULT_AGY_MODEL),
            effort=effort or os.environ.get("AR_AGY_EFFORT", DEFAULT_AGY_EFFORT),
            mode=selected_mode,
            sandbox=selected_sandbox,
            timeout_seconds=raw_timeout,
        )


@dataclass(frozen=True)
class AgyResult:
    status: str
    summary: str
    response: str
    return_code: int | None
    duration_seconds: float
    runtime: Mapping[str, Any]

    @property
    def passed(self) -> bool:
        return self.status == "PASS"

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": "agy-antigravity",
            "status": self.status,
            "summary": self.summary,
            "response": self.response,
            "return_code": self.return_code,
            "duration_seconds": self.duration_seconds,
            "runtime": dict(self.runtime),
        }


def build_agy_prompt(prompt: str) -> str:
    return (
        "You are the Google-stack scout/planner in a bounded subagent system. "
        "Focus on evidence-backed guidance for Google products and ecosystems "
        "such as Gemini, Firebase, Android, Google Cloud, browser/UI behavior, "
        "and frontend delivery. Inspect only what is needed. Do not edit files, "
        "commit, push, deploy, access credentials, or claim validation you did "
        "not perform. Return concise recommendations, risks, and concrete local "
        "checks.\n\nTask:\n" + prompt.strip()
    )


def _response_text(output: str) -> str:
    values: list[str] = []
    for line in output.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, Mapping):
            for key in ("response", "text", "message", "result"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    values.append(value.strip())
    return values[-1] if values else output.strip()


def run_agy(
    repo: str | Path,
    prompt: str,
    *,
    config: AgyConfig | None = None,
) -> AgyResult:
    root = Path(repo).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"AGY repository does not exist: {root}")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("AGY prompt must not be empty")
    selected = config or AgyConfig.from_env()
    command = [
        selected.executable,
        "--print",
        "--output-format",
        "json",
        "--model",
        selected.model,
        "--effort",
        selected.effort,
        "--mode",
        selected.mode,
        "--print-timeout",
        f"{int(selected.timeout_seconds)}s",
    ]
    if selected.sandbox:
        command.append("--sandbox")
    command.append(build_agy_prompt(prompt))
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=selected.timeout_seconds,
            check=False,
        )
        output = completed.stdout or ""
        stderr = completed.stderr or ""
        status = "PASS" if completed.returncode == 0 else "FAILED"
        return AgyResult(
            status=status,
            summary="Antigravity specialist completed" if status == "PASS" else "Antigravity specialist failed",
            response=_response_text(output)[-12000:],
            return_code=completed.returncode,
            duration_seconds=time.perf_counter() - started,
            runtime={
                "executable": selected.executable,
                "model": selected.model,
                "effort": selected.effort,
                "mode": selected.mode,
                "sandbox": selected.sandbox,
                "credential_mode": "agy-cli-session",
                "read_only_default": selected.mode == "plan",
                "stderr_tail": stderr[-2000:],
            },
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        return AgyResult(
            status="TIMEOUT",
            summary=f"Antigravity timed out after {selected.timeout_seconds:g} seconds",
            response=_response_text(stdout)[-4000:],
            return_code=None,
            duration_seconds=time.perf_counter() - started,
            runtime={
                "executable": selected.executable,
                "model": selected.model,
                "effort": selected.effort,
                "mode": selected.mode,
                "sandbox": selected.sandbox,
                "credential_mode": "agy-cli-session",
            },
        )
    except OSError as exc:
        return AgyResult(
            status="FAILED",
            summary=f"Antigravity could not start: {exc}",
            response="",
            return_code=None,
            duration_seconds=time.perf_counter() - started,
            runtime={
                "executable": selected.executable,
                "model": selected.model,
                "effort": selected.effort,
                "credential_mode": "agy-cli-session",
                "failure_kind": "agy_cli_unavailable",
            },
        )
