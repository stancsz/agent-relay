"""Bounded direct invocation adapters for local agent CLIs.

The durable coordinator remains the preferred path for repository edits.  This
module provides the small, synchronous MCP convenience path for callers that
need to ask an installed Gemini, Codex, or Claude Code CLI for one response.
Every invocation is bounded by a workspace root, timeout, output cap, and an
explicit read-only/write mode.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Mapping

from .agy_antigravity import AgyConfig, run_agy
from .env import load_dotenv
from .prompt_policy import with_high_agency_guidance


AGENT_NAMES = ("gemini", "codex", "claude")
AGENT_MODES = ("read-only", "workspace-write")
GEMINI_TRANSPORTS = ("auto", "gemini", "agy")
DEFAULT_AGENT_TIMEOUT_SECONDS = 300.0
DEFAULT_AGENT_MAX_OUTPUT_CHARS = 12_000
DEFAULT_AGENT_MAX_CONCURRENCY = 2
MAX_AGENT_TIMEOUT_SECONDS = 900.0
MAX_AGENT_PROMPT_CHARS = 24_000
MAX_AGENT_MODEL_CHARS = 200


class AgentInvocationError(ValueError):
    """Raised when a direct MCP agent invocation is not allowed."""


@dataclass(frozen=True)
class AgentInvocationConfig:
    """Configuration for the direct MCP agent surface.

    ``allow_workspace_writes`` is deliberately false by default.  The direct
    surface runs in the configured checkout, so callers needing edits should
    normally use the durable ``submit`` path instead.
    """

    workspace_root: Path
    timeout_seconds: float = DEFAULT_AGENT_TIMEOUT_SECONDS
    max_output_chars: int = DEFAULT_AGENT_MAX_OUTPUT_CHARS
    max_concurrency: int = DEFAULT_AGENT_MAX_CONCURRENCY
    allow_workspace_writes: bool = False
    gemini_bin: str | None = None
    codex_bin: str | None = None
    claude_bin: str | None = None
    gemini_transport: str = "auto"
    gemini_model: str = "gemini-3.1-pro-high"
    gemini_effort: str = "high"
    codex_model: str | None = None
    codex_effort: str = "high"
    claude_model: str | None = None
    claude_effort: str = "high"

    def __post_init__(self) -> None:
        root = Path(self.workspace_root).expanduser().resolve()
        if not root.is_dir():
            raise AgentInvocationError(f"agent workspace root is not a directory: {root}")
        object.__setattr__(self, "workspace_root", root)
        if not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= MAX_AGENT_TIMEOUT_SECONDS:
            raise AgentInvocationError(
                f"agent timeout must be between 0 and {MAX_AGENT_TIMEOUT_SECONDS:g} seconds"
            )
        if not 1_000 <= self.max_output_chars <= 32_000:
            raise AgentInvocationError("agent max output must be between 1000 and 32000 characters")
        if not 1 <= self.max_concurrency <= 8:
            raise AgentInvocationError("agent max concurrency must be between 1 and 8")
        if self.gemini_transport not in GEMINI_TRANSPORTS:
            raise AgentInvocationError(
                "gemini transport must be auto, gemini, or agy"
            )

    @classmethod
    def from_env(
        cls,
        *,
        workspace_root: str | Path | None = None,
        timeout_seconds: float | None = None,
        max_output_chars: int | None = None,
        max_concurrency: int | None = None,
        allow_workspace_writes: bool | None = None,
        gemini_bin: str | None = None,
        codex_bin: str | None = None,
        claude_bin: str | None = None,
        gemini_transport: str | None = None,
    ) -> "AgentInvocationConfig":
        load_dotenv()

        def env_float(name: str, fallback: float) -> float:
            raw = os.environ.get(name)
            if raw is None:
                return fallback
            try:
                return float(raw)
            except ValueError:
                return fallback

        def env_int(name: str, fallback: int) -> int:
            raw = os.environ.get(name)
            if raw is None:
                return fallback
            try:
                return int(raw)
            except ValueError:
                return fallback

        def env_bool(name: str, fallback: bool) -> bool:
            raw = os.environ.get(name)
            if raw is None:
                return fallback
            return raw.strip().lower() in {"1", "true", "yes", "on"}

        root_value = workspace_root or os.environ.get("AR_MCP_AGENT_REPO") or Path.cwd()
        return cls(
            workspace_root=Path(root_value),
            timeout_seconds=(
                timeout_seconds
                if timeout_seconds is not None
                else env_float("AR_MCP_AGENT_TIMEOUT_SECONDS", DEFAULT_AGENT_TIMEOUT_SECONDS)
            ),
            max_output_chars=(
                max_output_chars
                if max_output_chars is not None
                else env_int("AR_MCP_AGENT_MAX_OUTPUT_CHARS", DEFAULT_AGENT_MAX_OUTPUT_CHARS)
            ),
            max_concurrency=(
                max_concurrency
                if max_concurrency is not None
                else env_int("AR_MCP_AGENT_CONCURRENCY", DEFAULT_AGENT_MAX_CONCURRENCY)
            ),
            allow_workspace_writes=(
                allow_workspace_writes
                if allow_workspace_writes is not None
                else env_bool("AR_MCP_ALLOW_AGENT_WRITES", False)
            ),
            gemini_bin=gemini_bin or os.environ.get("AR_GEMINI_BIN"),
            codex_bin=codex_bin or os.environ.get("AR_CODEX_BIN"),
            claude_bin=claude_bin or os.environ.get("AR_CLAUDE_BIN"),
            gemini_transport=gemini_transport or os.environ.get("AR_GEMINI_TRANSPORT", "auto"),
            gemini_model=(
                os.environ.get("AR_GEMINI_MODEL")
                or os.environ.get("AR_AGY_MODEL")
                or "gemini-3.1-pro-high"
            ),
            gemini_effort=os.environ.get("AR_AGY_EFFORT", "high"),
            codex_model=os.environ.get("AR_MCP_CODEX_MODEL") or None,
            codex_effort=os.environ.get("AR_MCP_CODEX_EFFORT", "high"),
            claude_model=os.environ.get("AR_MCP_CLAUDE_MODEL") or None,
            claude_effort=os.environ.get("AR_MCP_CLAUDE_EFFORT", "high"),
        )


@dataclass(frozen=True)
class AgentInvocationResult:
    """Bounded, transport-aware result returned by an agent CLI."""

    agent: str
    transport: str
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
            "agent": self.agent,
            "transport": self.transport,
            "status": self.status,
            "summary": self.summary,
            "response": self.response,
            "return_code": self.return_code,
            "duration_seconds": self.duration_seconds,
            "runtime": dict(self.runtime),
        }


def _resolve_executable(override: str | None, env_name: str, candidates: tuple[str, ...]) -> str | None:
    selected = override or os.environ.get(env_name)
    if selected:
        return shutil.which(selected) or selected
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def _gemini_executable(config: AgentInvocationConfig) -> str | None:
    return _resolve_executable(
        config.gemini_bin,
        "AR_GEMINI_BIN",
        ("gemini.cmd", "gemini", "gemini.exe"),
    )


def _agy_executable() -> str | None:
    return _resolve_executable(None, "AR_AGY_BIN", ("agy.exe", "agy"))


def _codex_executable(config: AgentInvocationConfig) -> str | None:
    return _resolve_executable(
        config.codex_bin,
        "AR_CODEX_BIN",
        ("codex.cmd", "codex", "codex.exe"),
    )


def _claude_executable(config: AgentInvocationConfig) -> str | None:
    return _resolve_executable(
        config.claude_bin,
        "AR_CLAUDE_BIN",
        ("claude.cmd", "claude", "claude.exe"),
    )


def _gemini_transport(config: AgentInvocationConfig) -> tuple[str, str | None]:
    direct = _gemini_executable(config)
    agy = _agy_executable()
    if config.gemini_transport == "gemini":
        return "gemini", direct
    if config.gemini_transport == "agy":
        return "agy", agy
    # Auto mode makes the logical Gemini lane useful on machines where the
    # direct Gemini CLI is installed but its workspace-GCA project is absent.
    # An explicit direct executable or project configuration opts into direct.
    direct_requested = bool(config.gemini_bin or os.environ.get("AR_GEMINI_BIN"))
    project_configured = bool(
        os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT_ID")
    )
    if direct and (direct_requested or project_configured):
        return "gemini", direct
    if agy:
        return "agy", agy
    return "gemini", direct


def _inside_root(root: Path, requested: str | Path | None) -> Path:
    candidate = Path(requested).expanduser() if requested is not None else root
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise AgentInvocationError(
            f"workdir must remain inside the configured agent workspace root: {root}"
        ) from exc
    if not resolved.is_dir():
        raise AgentInvocationError(f"workdir is not an existing directory: {resolved}")
    return resolved


def _safe_text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentInvocationError(f"{label} must be a non-empty string")
    value = value.strip()
    if len(value) > limit:
        raise AgentInvocationError(f"{label} exceeds the {limit}-character limit")
    return value


def _policy_prompt(prompt: str, *, workdir: Path, mode: str) -> str:
    if mode == "read-only":
        policy = (
            "Execution policy: work read-only. Do not edit, create, delete, or rename files; "
            "do not commit, push, deploy, or change configuration. Report only evidence you observed."
        )
    else:
        policy = (
            f"Execution policy: edits are allowed only under {workdir}. Do not commit, push, "
            "deploy, or access credentials. Report every changed path and verification performed."
        )
    return with_high_agency_guidance(f"{policy}\n\nTask:\n{prompt}")


def _bounded(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    marker = "\n[agent output truncated]"
    return value[: max(0, limit - len(marker))] + marker, True


def _json_objects(output: str) -> list[Mapping[str, Any]]:
    objects: list[Mapping[str, Any]] = []
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            objects.append(value)
    if not objects:
        try:
            value = json.loads(output.strip())
        except (TypeError, json.JSONDecodeError):
            return []
        if isinstance(value, Mapping):
            objects.append(value)
    return objects


def _response_from_output(output: str) -> str:
    candidates: list[str] = []
    for value in _json_objects(output):
        item = value.get("item")
        if isinstance(item, Mapping):
            item_text = item.get("text")
            if isinstance(item_text, str) and item_text.strip():
                candidates.append(item_text.strip())
        for key in ("response", "result", "text", "message", "content"):
            item_value = value.get(key)
            if isinstance(item_value, str) and item_value.strip():
                candidates.append(item_value.strip())
            elif isinstance(item_value, Mapping):
                nested = item_value.get("text") or item_value.get("content")
                if isinstance(nested, str) and nested.strip():
                    candidates.append(nested.strip())
    return candidates[-1] if candidates else output.strip()


def _terminate(process: subprocess.Popen[str]) -> None:
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=2)
    except (subprocess.TimeoutExpired, OSError):
        pass
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )


def _run_command(
    *,
    agent: str,
    transport: str,
    command: list[str],
    workdir: Path,
    timeout_seconds: float,
    max_output_chars: int,
    model: str | None,
    mode: str,
    stdin_text: str | None = None,
) -> AgentInvocationResult:
    started = time.perf_counter()
    runtime: dict[str, Any] = {
        "executable": command[0],
        "command": command[1:],
        "model": model,
        "mode": mode,
        "read_only": mode == "read-only",
        "workdir": str(workdir),
        "credential_mode": f"{transport}-cli-session",
    }
    try:
        process = subprocess.Popen(
            command,
            cwd=workdir,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            stdout, stderr = process.communicate(input=stdin_text, timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _terminate(process)
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            response, truncated = _bounded(_response_from_output(stdout), min(max_output_chars, 4_000))
            runtime.update({"stderr_tail": stderr[-2_000:], "output_truncated": truncated})
            return AgentInvocationResult(
                agent=agent,
                transport=transport,
                status="TIMEOUT",
                summary=f"{agent} timed out after {timeout_seconds:g} seconds",
                response=response,
                return_code=None,
                duration_seconds=time.perf_counter() - started,
                runtime=runtime,
            )
    except OSError as exc:
        runtime["failure_kind"] = "agent_cli_unavailable"
        return AgentInvocationResult(
            agent=agent,
            transport=transport,
            status="FAILED",
            summary=f"{agent} could not start: {exc}",
            response="",
            return_code=None,
            duration_seconds=time.perf_counter() - started,
            runtime=runtime,
        )

    response, truncated = _bounded(_response_from_output(stdout), max_output_chars)
    runtime.update({"stderr_tail": (stderr or "")[-2_000:], "output_truncated": truncated})
    passed = process.returncode == 0 and bool(response.strip())
    if process.returncode != 0:
        summary = f"{agent} exited with code {process.returncode}"
    elif not response.strip():
        summary = f"{agent} returned an empty response"
    else:
        summary = f"{agent} agent completed"
    return AgentInvocationResult(
        agent=agent,
        transport=transport,
        status="PASS" if passed else "FAILED",
        summary=summary,
        response=response,
        return_code=process.returncode,
        duration_seconds=time.perf_counter() - started,
        runtime=runtime,
    )


class AgentInvoker:
    """Resolve, describe, and invoke the three supported local agent lanes."""

    def __init__(self, config: AgentInvocationConfig) -> None:
        self.config = config

    def status(self) -> dict[str, Any]:
        gemini_transport, gemini_executable = _gemini_transport(self.config)
        codex_executable = _codex_executable(self.config)
        claude_executable = _claude_executable(self.config)
        records = [
            {
                "agent": "gemini",
                "transport": gemini_transport,
                "executable": gemini_executable,
                "available": bool(gemini_executable),
                "proof": "executable-discovered-only",
            },
            {
                "agent": "codex",
                "transport": "codex-cli",
                "executable": codex_executable,
                "available": bool(codex_executable),
                "proof": "executable-discovered-only",
            },
            {
                "agent": "claude",
                "transport": "claude-cli",
                "executable": claude_executable,
                "available": bool(claude_executable),
                "proof": "executable-discovered-only",
            },
        ]
        for record in records:
            record["readiness"] = "ready" if record["available"] else "blocked"
        return {
            "workspace_root": str(self.config.workspace_root),
            "allow_workspace_writes": self.config.allow_workspace_writes,
            "agents": records,
        }

    def invoke(
        self,
        agent: str,
        prompt: str,
        *,
        workdir: str | Path | None = None,
        mode: str = "read-only",
        model: str | None = None,
        timeout_seconds: float | None = None,
    ) -> AgentInvocationResult:
        if agent not in AGENT_NAMES:
            raise AgentInvocationError(f"agent must be one of: {', '.join(AGENT_NAMES)}")
        if mode not in AGENT_MODES:
            raise AgentInvocationError("mode must be read-only or workspace-write")
        if mode == "workspace-write" and not self.config.allow_workspace_writes:
            raise AgentInvocationError(
                "workspace-write is disabled; start the MCP with --allow-agent-writes "
                "or use the durable submit path"
            )
        prompt = _safe_text(prompt, "prompt", MAX_AGENT_PROMPT_CHARS)
        try:
            selected_timeout = (
                self.config.timeout_seconds
                if timeout_seconds is None
                else float(timeout_seconds)
            )
        except (TypeError, ValueError) as exc:
            raise AgentInvocationError("timeout_seconds must be a number") from exc
        if not math.isfinite(selected_timeout) or not 0 < selected_timeout <= self.config.timeout_seconds:
            raise AgentInvocationError(
                f"timeout_seconds must be between 0 and {self.config.timeout_seconds:g}"
            )
        selected_workdir = _inside_root(self.config.workspace_root, workdir)
        if model is not None:
            model = _safe_text(model, "model", MAX_AGENT_MODEL_CHARS)
        task_prompt = _policy_prompt(prompt, workdir=selected_workdir, mode=mode)

        if agent == "gemini":
            transport, executable = _gemini_transport(self.config)
            selected_model = model or self.config.gemini_model
            if not executable:
                return AgentInvocationResult(
                    agent=agent,
                    transport=transport,
                    status="FAILED",
                    summary=f"{transport} executable was not found",
                    response="",
                    return_code=None,
                    duration_seconds=0.0,
                    runtime={"failure_kind": "agent_cli_unavailable", "read_only": mode == "read-only"},
                )
            if transport == "agy":
                result = run_agy(
                    selected_workdir,
                    task_prompt,
                    config=AgyConfig.from_env(
                        executable=executable,
                        model=selected_model,
                        effort=self.config.gemini_effort,
                        mode="plan" if mode == "read-only" else "accept-edits",
                        sandbox=mode == "read-only",
                        timeout_seconds=selected_timeout,
                    ),
                )
                response, truncated = _bounded(result.response, self.config.max_output_chars)
                runtime = dict(result.runtime)
                runtime["output_truncated"] = truncated
                return AgentInvocationResult(
                    agent=agent,
                    transport="agy",
                    status=result.status,
                    summary=result.summary,
                    response=response,
                    return_code=result.return_code,
                    duration_seconds=result.duration_seconds,
                    runtime=runtime,
                )
            command = [
                executable,
                "--output-format",
                "text",
                "--model",
                selected_model,
                "--approval-mode",
                "plan" if mode == "read-only" else "auto_edit",
            ]
            return _run_command(
                agent=agent,
                transport="gemini",
                command=command,
                workdir=selected_workdir,
                timeout_seconds=selected_timeout,
                max_output_chars=self.config.max_output_chars,
                model=selected_model,
                mode=mode,
                stdin_text=task_prompt,
            )

        if agent == "codex":
            executable = _codex_executable(self.config)
            if not executable:
                return AgentInvocationResult(
                    agent=agent,
                    transport="codex-cli",
                    status="FAILED",
                    summary="codex executable was not found",
                    response="",
                    return_code=None,
                    duration_seconds=0.0,
                    runtime={"failure_kind": "agent_cli_unavailable", "read_only": mode == "read-only"},
                )
            selected_model = model or self.config.codex_model
            command = [
                executable,
                "exec",
                "--cd",
                str(selected_workdir),
                "--sandbox",
                "read-only" if mode == "read-only" else "workspace-write",
                "--ephemeral",
                "--color",
                "never",
                "--json",
            ]
            if selected_model:
                command.extend(["--model", selected_model])
            command.append("-")
            return _run_command(
                agent=agent,
                transport="codex-cli",
                command=command,
                workdir=selected_workdir,
                timeout_seconds=selected_timeout,
                max_output_chars=self.config.max_output_chars,
                model=selected_model,
                mode=mode,
                stdin_text=task_prompt,
            )

        executable = _claude_executable(self.config)
        if not executable:
            return AgentInvocationResult(
                agent=agent,
                transport="claude-cli",
                status="FAILED",
                summary="claude executable was not found",
                response="",
                return_code=None,
                duration_seconds=0.0,
                runtime={"failure_kind": "agent_cli_unavailable", "read_only": mode == "read-only"},
            )
        selected_model = model or self.config.claude_model
        command = [
            executable,
            "--print",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--permission-mode",
            "plan" if mode == "read-only" else "acceptEdits",
            "--effort",
            self.config.claude_effort,
        ]
        if mode == "read-only":
            command.extend(["--allowed-tools", "Read"])
        if selected_model:
            command.extend(["--model", selected_model])
        return _run_command(
            agent=agent,
            transport="claude-cli",
            command=command,
            workdir=selected_workdir,
            timeout_seconds=selected_timeout,
            max_output_chars=self.config.max_output_chars,
            model=selected_model,
            mode=mode,
            stdin_text=task_prompt,
        )
