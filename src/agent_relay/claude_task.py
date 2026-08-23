"""Safe Claude task lane backed by the vendored bounded A2A bridge.

The bridge itself is intentionally workspace-oriented.  Agent Relay wraps it
with the same disposable Git sandbox and parent-owned verification contract as
the local-Qwen lane, so selecting Claude never grants it the caller's real
checkout implicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .patch import capture_diff, changed_files, worktree_status
from .result import DelegationResult, ResultStatus
from .sandbox import GitSandbox, SandboxError
from .task import DelegationTask, context_path_and_range, normalize_relative_path
from .verifier import run_verification


class ClaudeTaskError(RuntimeError):
    """Raised when the bounded Claude task transport cannot start or finish."""


@dataclass(frozen=True)
class ClaudeTaskConfig:
    executable: str | None = None
    bridge_script: Path | None = None
    timeout_seconds: float = 300.0
    verification_timeout_seconds: float = 120.0
    remote_url: str | None = None
    remote_auth_token: str | None = None
    remote_workspace_path: str = "."

    @classmethod
    def from_env(
        cls,
        *,
        executable: str | None = None,
        bridge_script: str | Path | None = None,
        timeout_seconds: float | None = None,
    ) -> "ClaudeTaskConfig":
        selected_timeout = timeout_seconds
        if selected_timeout is None:
            try:
                selected_timeout = float(os.environ.get("AR_CLAUDE_TIMEOUT_SECONDS", "300"))
            except ValueError:
                selected_timeout = 300.0
        if selected_timeout <= 0:
            raise ValueError("Claude task timeout must be greater than zero")

        selected_script = bridge_script or os.environ.get("AR_CLAUDE_BRIDGE_SCRIPT")
        if selected_script is None:
            selected_script = (
                Path(__file__).resolve().parents[2]
                / "lanes"
                / "claude-task"
                / "scripts"
                / "claude_a2a_server.py"
            )
        return cls(
            executable=executable or os.environ.get("AR_CLAUDE_BIN"),
            bridge_script=Path(selected_script).expanduser().resolve(),
            timeout_seconds=selected_timeout,
            verification_timeout_seconds=float(
                os.environ.get("AR_CLAUDE_VERIFICATION_TIMEOUT_SECONDS", "120")
            ),
            remote_url=(
                os.environ.get("AR_CLAUDE_A2A_SERVER_URL")
                or os.environ.get("CLAUDE_A2A_SERVER_URL")
            ),
            remote_auth_token=(
                os.environ.get("AR_CLAUDE_A2A_AUTH_TOKEN")
                or os.environ.get("CLAUDE_A2A_AUTH_TOKEN")
            ),
            remote_workspace_path=os.environ.get("AR_CLAUDE_A2A_WORKSPACE_PATH", "."),
        )


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _packet_digest(packet: Mapping[str, Any]) -> str:
    unsigned = dict(packet)
    unsigned.pop("context_digest", None)
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def _context_inputs(repo: Path, task: DelegationTask) -> list[dict[str, str]]:
    inputs: list[dict[str, str]] = []
    for spec in task.context:
        path, start, end = context_path_and_range(spec)
        source = repo / path
        if not source.is_file():
            continue
        raw = source.read_text(encoding="utf-8", errors="replace")
        lines = raw.splitlines()
        if start is not None:
            selected = lines[start - 1 : end]
            excerpt = "\n".join(selected)
        else:
            excerpt = raw
        inputs.append(
            {
                "path": path,
                "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                "excerpt": excerpt[:12_000],
            }
        )
    return inputs[:16]


def build_claude_task_packet(
    repo: Path,
    task: DelegationTask,
    *,
    workspace_path: str = ".",
) -> dict[str, Any]:
    """Translate the canonical Agent Relay contract to claude-a2a/0.1."""

    normalized_workspace = "." if workspace_path in {"", "."} else normalize_relative_path(workspace_path)

    acceptance = list(task.success_criteria or task.requirements)
    if not acceptance:
        acceptance = ["The bounded task completes and the parent reruns declared verification."]
    constraints = list(task.constraints)
    constraints.extend(
        [
            "Do not commit, push, merge, deploy, reset, clean, or switch branches.",
            "Only modify files listed in workspace.target_paths.",
        ]
    )
    packet: dict[str, Any] = {
        "protocol": "claude-a2a/0.1",
        "task_id": task.task_id,
        "caller_role": "orchestrator",
        "target_role": "worker",
        "operation": "work",
        "workspace": {"path": normalized_workspace, "target_paths": list(task.allowed_files)},
        "objective": task.objective,
        "acceptance_criteria": acceptance[:12],
        "constraints": constraints[:16],
        "verification": list(task.verification)[:12],
        "inputs": _context_inputs(repo, task),
        "profile": "agent-relay",
    }
    packet["context_digest"] = _packet_digest(packet)
    return packet


def _request_json(
    url: str,
    method: str,
    body: bytes | None = None,
    timeout: float = 10.0,
    *,
    auth_token: str | None = None,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    request = Request(url, method=method, data=body, headers=headers)
    try:
        context = None
        if url.lower().startswith("https://"):
            context = ssl.create_default_context(
                cafile=os.environ.get("AR_CLAUDE_A2A_CA_CERT")
                or os.environ.get("CLAUDE_A2A_CA_CERT")
            )
        with urlopen(request, timeout=timeout, context=context) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ClaudeTaskError(f"Claude bridge HTTP {exc.code}: {detail[:500]}") from exc
    except (OSError, URLError, json.JSONDecodeError) as exc:
        raise ClaudeTaskError(f"Claude bridge request {method} {url} failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise ClaudeTaskError("Claude bridge returned a non-object JSON response")
    return payload


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _python_command() -> list[str]:
    launcher = shutil.which("py.exe") or shutil.which("py")
    if launcher and os.name == "nt":
        return [launcher, "-3", "-B"]
    return [sys.executable, "-B"]


def _start_bridge(config: ClaudeTaskConfig, workspace: Path, state_dir: Path) -> tuple[subprocess.Popen[str], str]:
    if config.bridge_script is None or not config.bridge_script.is_file():
        raise ClaudeTaskError(
            "Claude A2A bridge is unavailable; set AR_CLAUDE_BRIDGE_SCRIPT to a valid server script"
        )
    port = _free_port()
    command = [
        *_python_command(),
        str(config.bridge_script),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--workspace-root",
        str(workspace),
        "--cli-fallback",
        "--state-dir",
        str(state_dir),
        "--timeout-seconds",
        str(max(1, int(config.timeout_seconds))),
    ]
    environment = dict(os.environ)
    if config.executable:
        environment["AR_CLAUDE_BIN"] = config.executable
    # The bridge logs every HTTP request. Keeping stderr as an undrained PIPE
    # lets a long async job fill the OS pipe and deadlock the HTTP server,
    # which then looks like a false /a2a/jobs liveness failure to the worker.
    # Persist it in the disposable task state directory instead; this keeps
    # diagnostics available without making transport progress depend on a
    # finite pipe buffer.
    stderr_log = state_dir / "bridge.stderr.log"
    stderr_handle = stderr_log.open("w", encoding="utf-8", errors="replace")
    try:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr_handle,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )
    finally:
        stderr_handle.close()
    setattr(process, "_agent_relay_stderr_log", stderr_log)
    _attach_windows_kill_on_close_job(process)
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + min(15.0, config.timeout_seconds)
    last_error = ""
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stderr = _bridge_stderr_tail(process)
                raise ClaudeTaskError(f"Claude bridge exited during startup: {stderr}")
            try:
                health = _request_json(f"{base}/health", "GET", timeout=1.0)
                if health.get("healthy"):
                    return process, base
                last_error = str(health)
            except ClaudeTaskError as exc:
                last_error = str(exc)
            time.sleep(0.1)
        raise ClaudeTaskError(f"Claude bridge did not become healthy: {last_error}")
    except Exception:
        _stop_bridge(process)
        raise


def _bridge_stderr_tail(process: subprocess.Popen[str], limit: int = 2_000) -> str:
    path = getattr(process, "_agent_relay_stderr_log", None)
    if not isinstance(path, Path):
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def _stop_bridge(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if os.name == "nt" and process.pid:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            else:
                process.kill()
            process.wait(timeout=3)
    _close_windows_kill_on_close_job(process)


def _attach_windows_kill_on_close_job(process: subprocess.Popen[str]) -> None:
    """Contain bridge descendants so an interrupted worker cannot leak them."""

    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ) or not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle)):
            kernel32.CloseHandle(job)
            return
        setattr(process, "_agent_relay_job_handle", job)
    except (AttributeError, OSError, TypeError, ValueError):
        # The explicit process-tree cleanup remains the fallback on hosts
        # where Job Objects are unavailable or the worker is already nested.
        return


def _close_windows_kill_on_close_job(process: subprocess.Popen[str]) -> None:
    job = getattr(process, "_agent_relay_job_handle", None)
    if job is None or os.name != "nt":
        return
    try:
        import ctypes

        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(job)
    except (AttributeError, OSError, TypeError):
        pass
    finally:
        try:
            delattr(process, "_agent_relay_job_handle")
        except AttributeError:
            pass


def _map_status(status: str) -> ResultStatus:
    return {
        "done": ResultStatus.SUCCESS,
        "blocked": ResultStatus.BLOCKED,
        "partial": ResultStatus.FAILED_VERIFICATION,
        "failed": ResultStatus.WORKER_ERROR,
    }.get(status, ResultStatus.WORKER_ERROR)


def _transport_failure_metadata(error: ClaudeTaskError) -> dict[str, Any]:
    """Classify bridge failures that are safe to retry in a fresh sandbox."""

    detail = str(error)
    retryable = any(
        marker in detail.lower()
        for marker in (
            "timed out",
            "did not become healthy",
            "http 502",
            "http 503",
            "http 504",
            "connection reset",
            "connection refused",
        )
    )
    return {
        "failure_kind": "bridge_transport",
        "retryable": retryable,
        "adapter_error": detail[:2_000],
    }


def _run_async_bridge_job(
    base: str,
    packet: Mapping[str, Any],
    *,
    timeout_seconds: float,
    cancel_event: threading.Event,
    auth_token: str | None = None,
) -> dict[str, Any]:
    """Run through the bridge's durable job API so cancellation is real."""

    request_options: dict[str, Any] = {}
    if auth_token:
        request_options["auth_token"] = auth_token

    submitted = _request_json(
        f"{base}/a2a/jobs",
        "POST",
        json.dumps(packet, ensure_ascii=False).encode("utf-8"),
        timeout=10.0,
        **request_options,
    )
    job_id = submitted.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise ClaudeTaskError("Claude bridge did not return a durable job_id")
    deadline = time.monotonic() + timeout_seconds + 15.0
    cancel_sent = False
    terminal = {"done", "failed", "blocked", "cancelled", "interrupted"}
    while time.monotonic() < deadline:
        if cancel_event.is_set() and not cancel_sent:
            cancel_options = {"timeout": 10.0, **request_options}
            _request_json(f"{base}/a2a/jobs/{job_id}/cancel", "POST", b"{}", **cancel_options)
            cancel_sent = True
        summary_options = {"timeout": 10.0, **request_options}
        summary = _request_json(f"{base}/a2a/jobs/{job_id}", "GET", **summary_options)
        status = str(summary.get("status", ""))
        if status in terminal:
            result = summary.get("result")
            if isinstance(result, Mapping):
                merged = dict(result)
                # The durable job state is authoritative at the transport
                # boundary. A cancelled subprocess may leave a nested
                # adapter result marked failed; exposing that inner status
                # would erase proof that the stop boundary was reached.
                merged["status"] = status
                merged["job"] = summary
                return merged
            return summary
        time.sleep(0.2)
    raise ClaudeTaskError(f"Claude bridge job {job_id} timed out")


def _remote_result(
    task: DelegationTask,
    response: Mapping[str, Any],
    *,
    endpoint: str,
    started: float,
) -> DelegationResult:
    status = _map_status(str(response.get("status", "failed")))
    changed = tuple(
        item for item in response.get("changed_paths", ())
        if isinstance(item, str)
    )
    blockers: tuple[str, ...] = ()
    if status is not ResultStatus.SUCCESS:
        blockers = (str(response.get("output", "remote Claude task failed"))[:1000],)
    return DelegationResult(
        task_id=task.task_id,
        status=status,
        summary=str(response.get("output", "remote Claude task completed"))[:2000],
        files_changed=changed,
        patch=str(response.get("patch", "")),
        blockers=blockers,
        attempts=1,
        duration_seconds=time.perf_counter() - started,
        sandbox_mode="remote-agent-owned",
        metadata={
            "lane": "claude-task",
            "transport": "remote-claude-a2a",
            "remote_endpoint": endpoint,
            "remote_workspace_path": response.get("workspace_path"),
            "server_receipt": response.get("server_receipt", {}),
            "main_worktree_unchanged": True,
            "verification_authority": "remote-worker-receipt",
            "job": response.get("job", {}),
        },
    )


def _run_remote_claude_task(
    task: DelegationTask,
    repo: Path,
    config: ClaudeTaskConfig,
    *,
    cancel_event: threading.Event | None,
) -> DelegationResult:
    started = time.perf_counter()
    assert config.remote_url is not None
    endpoint = config.remote_url.rstrip("/")
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ClaudeTaskError("remote Claude A2A URL must be an absolute http(s) URL")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ClaudeTaskError("remote Claude A2A HTTP is limited to loopback; use HTTPS or a secure tunnel")
    packet = build_claude_task_packet(
        repo,
        task,
        workspace_path=config.remote_workspace_path,
    )
    response = _run_async_bridge_job(
        endpoint,
        packet,
        timeout_seconds=config.timeout_seconds,
        cancel_event=cancel_event or threading.Event(),
        auth_token=config.remote_auth_token,
    )
    return _remote_result(task, response, endpoint=endpoint, started=started)


def run_claude_task(
    task: DelegationTask,
    repo: str | Path,
    *,
    config: ClaudeTaskConfig | None = None,
    cancel_event: threading.Event | None = None,
) -> DelegationResult:
    """Run one Claude task in a disposable sandbox and return a normal result."""

    source_repo = Path(repo).resolve()
    started = time.perf_counter()
    bridge: subprocess.Popen[str] | None = None
    state_dir: Path | None = None
    try:
        selected = config or ClaudeTaskConfig.from_env()
        if selected.remote_url:
            return _run_remote_claude_task(
                task,
                source_repo,
                selected,
                cancel_event=cancel_event,
            )
        state_dir = Path(tempfile.mkdtemp(prefix="ar-claude-state-"))
        with GitSandbox(source_repo, task.task_id) as sandbox:
            assert sandbox.path is not None
            bridge, base = _start_bridge(selected, sandbox.path, state_dir)
            packet = build_claude_task_packet(sandbox.path, task)
            if cancel_event is None:
                response = _request_json(
                    f"{base}/a2a/tasks",
                    "POST",
                    json.dumps(packet, ensure_ascii=False).encode("utf-8"),
                    timeout=selected.timeout_seconds + 10,
                )
            else:
                response = _run_async_bridge_job(
                    base,
                    packet,
                    timeout_seconds=selected.timeout_seconds,
                    cancel_event=cancel_event,
                )
            status = _map_status(str(response.get("status", "failed")))
            execution_stopped = str(response.get("status", "")) == "cancelled"
            changed = changed_files(sandbox.path)
            unexpected = sorted(set(changed) - set(task.allowed_files))
            verification = run_verification(
                task.verification,
                sandbox.path,
                timeout_seconds=selected.verification_timeout_seconds,
            )
            # Verification commands are allowed to create caches and reports.
            # Remove predictable artifacts, then take the authoritative final
            # scope snapshot so the receipt covers both worker and verifier.
            sandbox.clean_verification_artifacts()
            changed = changed_files(sandbox.path)
            unexpected = sorted(set(changed) - set(task.allowed_files))
            if unexpected:
                status = ResultStatus.SCOPE_VIOLATION
            elif status is ResultStatus.SUCCESS and any(not item.passed for item in verification):
                status = ResultStatus.FAILED_VERIFICATION
            patch = capture_diff(sandbox.path)
            blockers = []
            if unexpected:
                blockers.append(f"Claude changed files outside scope: {', '.join(unexpected)}")
            if status is ResultStatus.BLOCKED:
                blockers.append(str(response.get("output", "Claude task was blocked"))[:1000])
            return DelegationResult(
                task_id=task.task_id,
                status=status,
                summary=str(response.get("output", "Claude task completed"))[:2000],
                files_changed=changed,
                patch=patch,
                verification=verification,
                blockers=tuple(item for item in blockers if item),
                attempts=1,
                duration_seconds=time.perf_counter() - started,
                sandbox_mode=sandbox.mode,
                metadata={
                    "lane": "claude-task",
                    "transport": "claude-a2a-cli-fallback-or-native",
                    "server_receipt": response.get("server_receipt", {}),
                    "main_worktree_unchanged": True,
                    "claude_packet_digest": packet["context_digest"],
                    "worktree_status": list(worktree_status(sandbox.path)),
                    "execution_stopped": execution_stopped,
                },
            )
    except (ClaudeTaskError, OSError, SandboxError, ValueError) as exc:
        metadata = {"lane": "claude-task", "main_worktree_unchanged": True}
        if isinstance(exc, ClaudeTaskError):
            metadata.update(_transport_failure_metadata(exc))
        return DelegationResult(
            task_id=task.task_id,
            status=ResultStatus.WORKER_ERROR,
            summary=f"Claude task could not run: {exc}",
            blockers=(str(exc),),
            attempts=0,
            duration_seconds=time.perf_counter() - started,
            metadata=metadata,
        )
    finally:
        if bridge is not None:
            _stop_bridge(bridge)
        if state_dir is not None:
            shutil.rmtree(state_dir, ignore_errors=True)
