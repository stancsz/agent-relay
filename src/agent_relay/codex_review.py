"""Read-only Codex subscription verifier lane.

This adapter intentionally invokes the installed Codex CLI instead of an API
key or an Ollama provider.  Authentication therefore remains in the user's
existing Codex login/session, while the adapter owns the model, effort, scope,
timeout, and bounded receipt.
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

from .prompt_policy import with_high_agency_guidance

from .env import load_dotenv


DEFAULT_REVIEW_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING_EFFORT = "high"


@dataclass(frozen=True)
class CodexReviewConfig:
    executable: str
    model: str = DEFAULT_REVIEW_MODEL
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    timeout_seconds: float = 300.0

    @classmethod
    def from_env(
        cls,
        *,
        executable: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: float | None = None,
    ) -> "CodexReviewConfig":
        load_dotenv()
        resolved = (
            executable
            or os.environ.get("AR_CODEX_BIN")
            or shutil.which("codex.cmd")
            or shutil.which("codex")
        )
        if not resolved:
            raise FileNotFoundError(
                "Codex CLI was not found; install/login to Codex or set AR_CODEX_BIN"
            )
        raw_timeout = timeout_seconds
        if raw_timeout is None:
            try:
                raw_timeout = float(
                    os.environ.get("AR_CODEX_REVIEW_TIMEOUT_SECONDS", "300")
                )
            except ValueError:
                raw_timeout = 300.0
        if raw_timeout <= 0:
            raise ValueError("review timeout must be greater than zero")
        return cls(
            executable=resolved,
            model=model or os.environ.get("AR_CODEX_REVIEW_MODEL", DEFAULT_REVIEW_MODEL),
            reasoning_effort=(
                reasoning_effort
                or os.environ.get("AR_CODEX_REVIEW_REASONING_EFFORT", DEFAULT_REASONING_EFFORT)
            ),
            timeout_seconds=raw_timeout,
        )


@dataclass(frozen=True)
class CodexReviewResult:
    status: str
    summary: str
    findings: str
    return_code: int | None
    duration_seconds: float
    runtime: Mapping[str, Any]

    @property
    def passed(self) -> bool:
        return self.status == "PASS"

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": "sol-reviewer",
            "status": self.status,
            "summary": self.summary,
            "findings": self.findings,
            "return_code": self.return_code,
            "duration_seconds": self.duration_seconds,
            "runtime": dict(self.runtime),
        }


def _json_events(output: str) -> list[Mapping[str, Any]]:
    events: list[Mapping[str, Any]] = []
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            events.append(value)
    return events


def _last_message(output: str) -> str:
    messages: list[str] = []
    for event in _json_events(output):
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if isinstance(item, Mapping) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                messages.append(text.strip())
    return messages[-1] if messages else ""


def build_review_prompt(custom_prompt: str | None = None) -> str:
    base = (
        "Act as an independent QA verifier for this repository. Review the selected "
        "diff read-only. Do not edit files, run destructive commands, commit, push, "
        "or change configuration. Identify correctness bugs, regressions, missing "
        "tests, security risks, and scope violations. Start with findings ordered "
        "by severity and include file/line references when available. If no findings "
        "exist, say so and list the validation evidence you inspected."
    )
    prompt = (
        f"{base}\n\nAdditional review scope:\n{custom_prompt.strip()}"
        if custom_prompt and custom_prompt.strip()
        else base
    )
    return with_high_agency_guidance(prompt)


def run_codex_review(
    repo: str | Path,
    *,
    base: str | None = None,
    uncommitted: bool = True,
    prompt: str | None = None,
    config: CodexReviewConfig | None = None,
) -> CodexReviewResult:
    root = Path(repo).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"review repository does not exist: {root}")
    selected = config or CodexReviewConfig.from_env()
    command = [
        selected.executable,
        "exec",
        "review",
        "--json",
        "--model",
        selected.model,
        "-c",
        f'model_reasoning_effort="{selected.reasoning_effort}"',
    ]
    if base:
        command.extend(["--base", base])
    elif uncommitted:
        command.append("--uncommitted")
    started = time.perf_counter()
    # Codex CLI 0.87 treats the review selector (`--uncommitted` or `--base`)
    # and a positional custom prompt as mutually exclusive, despite both being
    # shown in help. Keep the selector authoritative and let Codex's built-in
    # review instructions run in that mode; custom instructions are forwarded
    # only when no selector is requested.
    prompt_args = [] if (base or uncommitted) else [build_review_prompt(prompt)]
    try:
        completed = subprocess.run(
            command + prompt_args,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=selected.timeout_seconds,
            check=False,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        findings = _last_message(stdout) or stdout.strip()
        status = "PASS" if completed.returncode == 0 else "FAILED"
        summary = "Codex review completed" if status == "PASS" else "Codex review failed"
        runtime: dict[str, Any] = {
            "executable": selected.executable,
            "model": selected.model,
            "reasoning_effort": selected.reasoning_effort,
            "credential_mode": "codex-cli-login",
            "read_only": True,
            "uncommitted": uncommitted if base is None else False,
            "base": base,
            "prompt_forwarded": bool(prompt_args),
            "stderr_tail": stderr[-2000:],
        }
        return CodexReviewResult(
            status=status,
            summary=summary,
            findings=findings[-12000:],
            return_code=completed.returncode,
            duration_seconds=time.perf_counter() - started,
            runtime=runtime,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        return CodexReviewResult(
            status="TIMEOUT",
            summary=f"Codex review timed out after {selected.timeout_seconds:g} seconds",
            findings=_last_message(stdout) or stdout[-4000:],
            return_code=None,
            duration_seconds=time.perf_counter() - started,
            runtime={
                "executable": selected.executable,
                "model": selected.model,
                "reasoning_effort": selected.reasoning_effort,
                "credential_mode": "codex-cli-login",
                "read_only": True,
                "timeout_seconds": selected.timeout_seconds,
            },
        )
    except OSError as exc:
        return CodexReviewResult(
            status="FAILED",
            summary=f"Codex review could not start: {exc}",
            findings="",
            return_code=None,
            duration_seconds=time.perf_counter() - started,
            runtime={
                "executable": selected.executable,
                "model": selected.model,
                "reasoning_effort": selected.reasoning_effort,
                "credential_mode": "codex-cli-login",
                "read_only": True,
                "failure_kind": "codex_cli_unavailable",
            },
        )
