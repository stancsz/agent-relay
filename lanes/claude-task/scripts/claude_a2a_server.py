"""HTTP A2A relay that gives each task a fresh Claude MCP session.

The relay is intentionally small and conservative: it accepts bounded task
envelopes, maps orchestrator requests to worker/verifier roles, starts one
short-lived `claude mcp serve` client session per task, and returns a receipt.
It is loopback-only by default. LAN binding requires an auth token.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import re
import time
from urllib.parse import parse_qs, urlsplit
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from a2a_protocol import MAX_PACKET_BYTES, MAX_TEXT_CHARS, ProtocolError, digest_without_context_digest, validate_task, validate_result
from bridge_state import ProfileStore, StateError, atomic_write, utc_epoch


def is_native_capability_failure(value: object) -> bool:
    """Recognize bounded native-runtime capability failures eligible for fallback."""
    message = str(value or "").lower()
    return any(marker in message for marker in (
        "agent type",
        "native agent teams are unavailable",
        "missing mcp tools",
        "does not expose the agent tool",
    ))


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def status_paths(status: str) -> set[str]:
    paths: set[str] = set()
    for line in status.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[-1]
        normalized = path.strip('"').replace("\\", "/").rstrip("/")
        if normalized:
            paths.add(normalized)
    return paths


def git_snapshot(root: Path, extra_paths: list[str] | None = None) -> dict[str, Any]:
    def git(*args: str) -> str:
        process = subprocess.run(["git", "-C", str(root), *args], text=True, encoding="utf-8", errors="replace", capture_output=True)
        if process.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {process.stderr.strip()}")
        return process.stdout.strip()

    status = git("status", "--porcelain=v1")
    untracked_hashes: dict[str, str] = {}
    for relative in git("ls-files", "--others", "--exclude-standard").splitlines():
        path = root / relative
        if path.is_file():
            untracked_hashes[relative.replace("\\", "/")] = hashlib.sha256(path.read_bytes()).hexdigest()
    target_fingerprints: dict[str, str] = {}
    for relative in extra_paths or []:
        normalized = str(relative).replace("\\", "/")
        path = (root / relative).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError:
            target_fingerprints[normalized] = "outside-workspace"
            continue
        target_fingerprints[normalized] = (
            hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file()
            else "missing"
        )
    fingerprint_payload = {
        "status": status,
        "unstaged_diff": hashlib.sha256(git("diff", "--binary").encode("utf-8", errors="replace")).hexdigest(),
        "staged_diff": hashlib.sha256(git("diff", "--cached", "--binary").encode("utf-8", errors="replace")).hexdigest(),
        "untracked": untracked_hashes,
        # Git status intentionally omits ignored files.  A task's explicit
        # target allowlist must still be observable for change expectations.
        "explicit_targets": target_fingerprints,
    }
    return {
        "head": git("rev-parse", "HEAD"),
        "status": status,
        "status_paths": sorted(status_paths(status)),
        "target_fingerprints": target_fingerprints,
        "change_fingerprint": hashlib.sha256(json_bytes(fingerprint_payload)).hexdigest(),
    }


def resolve_workspace(server_root: Path, relative_path: str) -> Path:
    candidate = (server_root / (relative_path or ".")).resolve()
    try:
        candidate.relative_to(server_root)
    except ValueError as exc:
        raise ProtocolError("workspace path escapes the server allowlist") from exc
    if not candidate.is_dir():
        raise ProtocolError("workspace path is not a directory")
    return candidate


def safe_team_name(task: dict[str, Any]) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", task["task_id"]).strip("-")[:40]
    return f"a2a-{base or 'task'}-{task['context_digest'][:8]}"


def safe_job_id(task: dict[str, Any]) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", task["task_id"]).strip("-")[:48] or "job"
    return f"{base}-{task['context_digest'][:12]}"


def is_client_disconnect(error: BaseException) -> bool:
    """Return true for socket errors caused by a caller closing early."""
    if isinstance(error, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)):
        return True
    return getattr(error, "winerror", None) in {10053, 10054}


def render_prompt(task: dict[str, Any], profile_context: dict[str, Any] | None = None) -> str:
    role = task["target_role"]
    role_rules = (
        "You are the worker. Make only the requested bounded implementation changes. Do not commit, push, merge, deploy, reset, clean, or switch branches."
        if role == "worker"
        else "You are the verifier. Do not edit files, commit, push, merge, deploy, reset, clean, or switch branches. Inspect the requested evidence independently."
    )
    inputs = []
    for item in task["inputs"]:
        inputs.append(f"- {item['path']} (sha256 {item['sha256']}):\n{item['excerpt']}")
    profile_lines: list[str] = []
    if profile_context:
        profile_lines = ["", "## Bounded profile context", f"Profile: {profile_context.get('profile', 'default')}"]
        for skill_ref, content in profile_context.get("skills", {}).items():
            profile_lines.extend([f"### Skill: {skill_ref}", content])
        for memory in profile_context.get("memories", []):
            profile_lines.extend([f"### Memory ({memory.get('kind', 'lesson')})", str(memory.get('text', ''))[:4000]])
    return "\n".join([
        "## Claude A2A isolated task",
        f"Task ID: {task['task_id']}",
        f"Context digest: {task['context_digest']}",
        f"Role: {role}",
        role_rules,
        "This is a fresh task session. Do not infer or request prior conversation context. Use only this packet and the repository files you need.",
        "",
        "## Objective",
        task["objective"],
        "",
        "## Workspace target paths",
        *[f"- {path}" for path in task["workspace"]["target_paths"]],
        "",
        "## Bounded inputs",
        *(inputs or ["- None supplied; inspect only the listed target paths."]),
        "",
        "## Acceptance criteria",
        *[f"- {criterion}" for criterion in task["acceptance_criteria"]],
        "",
        "## Constraints",
        *[f"- {constraint}" for constraint in task["constraints"]],
        *profile_lines,
        "",
        "Return a concise report of what you actually did, commands and exit codes, files changed, risks, and unmet criteria. Do not claim a check you did not run.",
    ])


@contextmanager
def isolated_cli_verifier_workspace(
    workspace: Path,
    include_paths: list[str] | None = None,
    heartbeat: Any = None,
):
    """Yield a disposable copy for a CLI verifier session.

    Claude's ``Bash`` tool is intentionally available to verifiers so they can
    run read-only evidence commands. The tool boundary is advisory, though: a
    verifier can still invoke a script that writes files. Running that session
    in the caller's dirty checkout would make a rejected verifier capable of
    changing user work. The copy is initialized as a temporary Git repository
    so the existing receipt/scope checks remain meaningful.
    """
    temp_root = Path(tempfile.mkdtemp(prefix="claude-a2a-verifier-"))
    verifier_workspace = temp_root / "workspace"
    try:
        if heartbeat:
            heartbeat()
        # ``[]`` is an explicit empty source set, not a request to clone the
        # caller.  Direct/team verifier packets with no target or input paths
        # otherwise fall through to the full dirty-worktree copy and can spend
        # the entire bounded setup window staging unrelated user files.
        if include_paths is not None:
            verifier_workspace.mkdir(parents=True, exist_ok=True)
            for relative in dict.fromkeys(include_paths):
                source = workspace / relative
                destination = verifier_workspace / relative
                if source.is_file():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
                elif source.is_dir():
                    shutil.copytree(source, destination, dirs_exist_ok=True)
                if heartbeat:
                    heartbeat()
        else:
            shutil.copytree(
                workspace,
                verifier_workspace,
                ignore=shutil.ignore_patterns(
                    ".git",
                    ".env",
                    ".env.*",
                    "__pycache__",
                    ".pytest_cache",
                    ".mypy_cache",
                    ".ruff_cache",
                ),
            )
            if heartbeat:
                heartbeat()
        for args in (
            ["init", "--quiet"],
            ["config", "user.name", "Agent Relay"],
            ["config", "user.email", "agent-relay@example.invalid"],
            ["add", "-A"],
            ["commit", "--quiet", "--allow-empty", "-m", "verifier baseline"],
        ):
            process = subprocess.run(
                ["git", *args],
                cwd=verifier_workspace,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=30,
            )
            if process.returncode != 0:
                detail = (process.stderr or process.stdout).strip()[:500]
                raise RuntimeError(f"temporary verifier git setup failed: {detail}")
            if heartbeat:
                heartbeat()
        yield verifier_workspace
    finally:
        def remove_readonly(function, path, _exc_info):
            try:
                os.chmod(path, stat.S_IWRITE)
                function(path)
            except OSError:
                pass

        try:
            shutil.rmtree(temp_root, onerror=remove_readonly)
        except OSError:
            # Cleanup is best effort; the verifier has already run outside
            # the caller workspace, so a locked temporary artifact is safe.
            pass


class A2AState:
    def __init__(self, args: argparse.Namespace) -> None:
        self.server_root = Path(args.workspace_root).resolve()
        self.auth_token = args.auth_token or os.environ.get("CLAUDE_A2A_AUTH_TOKEN")
        self.worker_agent_type = args.worker_agent_type
        self.verifier_agent_type = args.verifier_agent_type
        self.agents_json = getattr(args, "agents_json", None) or os.environ.get("CLAUDE_A2A_AGENTS_JSON")
        self.cli_fallback = bool(getattr(args, "cli_fallback", False) or os.environ.get("CLAUDE_A2A_CLI_FALLBACK"))
        self.auto_cli_fallback = not bool(getattr(args, "no_cli_fallback", False)) and str(os.environ.get("CLAUDE_A2A_AUTO_CLI_FALLBACK", "1")).lower() not in {"0", "false", "no"}
        self.timeout_seconds = args.timeout_seconds
        self.seen: dict[str, str] = {}
        self.workspace_locks: dict[str, threading.Lock] = {}
        self.lock = threading.Lock()
        self.client_script = Path(__file__).with_name("claude_mcp_delegate.py")
        state_value = getattr(args, "state_dir", None) or os.environ.get("CLAUDE_TEAM_BRIDGE_STATE_DIR")
        self.state_dir = Path(state_value).resolve() if state_value else None
        self.jobs_dir = self.state_dir / "jobs" if self.state_dir else None
        self.jobs: dict[str, dict[str, Any]] = {}
        self.job_threads: dict[str, threading.Thread] = {}
        self.job_cancel: dict[str, threading.Event] = {}
        self.schedules: dict[str, dict[str, Any]] = {}
        self.scheduler_thread: threading.Thread | None = None
        if self.state_dir:
            self._load_jobs()
            self._load_schedules()
            self.scheduler_thread = threading.Thread(target=self._schedule_loop, daemon=True, name="claude-team-scheduler")
            self.scheduler_thread.start()

    def _job_path(self, job_id: str) -> Path:
        if not self.jobs_dir:
            raise ProtocolError("durable jobs are disabled; start the relay with --state-dir")
        digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
        return self.jobs_dir / f"{digest}.json"

    def _persist_job(self, record: dict[str, Any]) -> None:
        if not self.jobs_dir:
            return
        atomic_write(self._job_path(record["job_id"]), json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))

    def _load_jobs(self) -> None:
        if not self.jobs_dir:
            return
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        for path in self.jobs_dir.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict) or not record.get("job_id") or not record.get("task"):
                continue
            self.jobs[record["job_id"]] = record
            self.seen[record["task"]["task_id"]] = record["task"]["context_digest"]
            if record.get("status") == "running":
                record["status"] = "interrupted"
                record["updated_at"] = utc_epoch()
                self._persist_job(record)
        for record in list(self.jobs.values()):
            if record.get("status") == "queued":
                self._start_job(record["job_id"])

    def profile_store(self, profile: str) -> ProfileStore:
        if not self.state_dir:
            raise ProtocolError("profiles and durable memory require --state-dir")
        return ProfileStore(self.state_dir, profile)

    def profile_context(self, task: dict[str, Any]) -> dict[str, Any]:
        if not self.state_dir:
            return {}
        store = self.profile_store(task.get("profile", "default"))
        skills = {}
        for skill_ref in task.get("skill_refs", []):
            content = store.read_skill(skill_ref)
            if content is not None:
                skills[skill_ref] = content[:4000]
        memories = store.search_memory(task.get("memory_query"), limit=4) if task.get("memory_query") else []
        return {"profile": task.get("profile", "default"), "skills": skills, "memories": memories}

    def list_profiles(self) -> list[str]:
        if not self.state_dir:
            return []
        root = self.state_dir / "profiles"
        return sorted(path.name for path in root.iterdir() if path.is_dir()) if root.is_dir() else []

    def _schedule_path(self, schedule_id: str) -> Path:
        if not self.state_dir:
            raise ProtocolError("schedules require --state-dir")
        digest = hashlib.sha256(schedule_id.encode("utf-8")).hexdigest()
        return self.state_dir / "schedules" / f"{digest}.json"

    def _persist_schedule(self, record: dict[str, Any]) -> None:
        atomic_write(self._schedule_path(record["schedule_id"]), json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))

    def _load_schedules(self) -> None:
        if not self.state_dir:
            return
        root = self.state_dir / "schedules"
        if not root.is_dir():
            return
        for path in root.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(record, dict) and record.get("schedule_id") and record.get("task"):
                self.schedules[record["schedule_id"]] = record

    def _schedule_loop(self) -> None:
        while True:
            now = utc_epoch()
            for record in list(self.schedules.values()):
                if not record.get("enabled", True) or float(record.get("next_run", 0)) > now:
                    continue
                task = json.loads(json.dumps(record["task"], ensure_ascii=False))
                task["task_id"] = f"{record['schedule_id']}-{int(now)}"[:120]
                task["context_digest"] = digest_without_context_digest(task)
                try:
                    validate_task(task)
                    self.enqueue_job(task)
                except (ProtocolError, ValueError, OSError) as exc:
                    record["last_error"] = str(exc)
                interval = record.get("interval_seconds")
                if interval:
                    record["next_run"] = now + float(interval)
                else:
                    record["enabled"] = False
                record["updated_at"] = now
                try:
                    self._persist_schedule(record)
                except OSError:
                    pass
            time.sleep(1)

    def create_schedule(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.state_dir:
            raise ProtocolError("schedules require --state-dir")
        if not isinstance(body, dict) or not isinstance(body.get("schedule_id"), str) or not re.fullmatch(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$", body["schedule_id"]):
            raise ProtocolError("schedule_id must be a safe identifier")
        task = validate_task(body.get("task"))
        interval = body.get("interval_seconds")
        run_at = body.get("run_at", utc_epoch())
        if interval is not None and (not isinstance(interval, (int, float)) or not 1 <= interval <= 31_536_000):
            raise ProtocolError("interval_seconds must be between 1 second and 365 days")
        if not isinstance(run_at, (int, float)) or run_at < utc_epoch() - 60:
            raise ProtocolError("run_at must be a current or future epoch timestamp")
        record = {
            "protocol": "claude-a2a/0.1",
            "schedule_id": body["schedule_id"],
            "task": task,
            "interval_seconds": interval,
            "next_run": float(run_at),
            "enabled": bool(body.get("enabled", True)),
            "created_at": utc_epoch(),
            "updated_at": utc_epoch(),
        }
        self.schedules[record["schedule_id"]] = record
        self._persist_schedule(record)
        return {key: value for key, value in record.items() if key != "task"} | {"task_id": task["task_id"], "context_digest": task["context_digest"]}

    def list_schedules(self) -> list[dict[str, Any]]:
        return [{key: value for key, value in record.items() if key != "task"} | {"task_id": record["task"]["task_id"], "context_digest": record["task"]["context_digest"]} for record in self.schedules.values()]

    def cancel_schedule(self, schedule_id: str) -> dict[str, Any]:
        record = self.schedules.get(schedule_id)
        if not record:
            raise ProtocolError("schedule not found")
        record["enabled"] = False
        record["updated_at"] = utc_epoch()
        self._persist_schedule(record)
        return {key: value for key, value in record.items() if key != "task"}

    def _job_summary(self, record: dict[str, Any]) -> dict[str, Any]:
        return {key: record.get(key) for key in ("job_id", "task_id", "goal_id", "context_digest", "status", "created_at", "updated_at", "heartbeat_at", "attempts", "cancel_requested", "result")}

    def enqueue_job(self, task: dict[str, Any]) -> dict[str, Any]:
        if not self.state_dir:
            raise ProtocolError("durable jobs are disabled; start the relay with --state-dir")
        resolve_workspace(self.server_root, task["workspace"]["path"])
        job_id = safe_job_id(task)
        with self.lock:
            existing = self.jobs.get(job_id)
            if existing:
                if existing.get("context_digest") != task["context_digest"]:
                    raise ProtocolError("job_id was already used with a different context digest")
                return self._job_summary(existing)
            record = {
                "protocol": task["protocol"],
                "job_id": job_id,
                "task_id": task["task_id"],
                "goal_id": task.get("goal_id"),
                "context_digest": task["context_digest"],
                "status": "queued",
                "created_at": utc_epoch(),
                "updated_at": utc_epoch(),
                "heartbeat_at": None,
                "attempts": 0,
                "cancel_requested": False,
                "task": task,
                "result": None,
            }
            self.jobs[job_id] = record
            self.seen[task["task_id"]] = task["context_digest"]
            self._persist_job(record)
            self._start_job(job_id)
            return self._job_summary(record)

    def _start_job(self, job_id: str) -> None:
        if job_id in self.job_threads and self.job_threads[job_id].is_alive():
            return
        cancel_event = threading.Event()
        self.job_cancel[job_id] = cancel_event
        thread = threading.Thread(target=self._run_job, args=(job_id, cancel_event), daemon=True, name=f"claude-team-job-{job_id[:24]}")
        self.job_threads[job_id] = thread
        thread.start()

    def _run_job(self, job_id: str, cancel_event: threading.Event) -> None:
        record = self.jobs.get(job_id)
        if not record:
            return
        with self.lock:
            if record.get("cancel_requested"):
                record["status"] = "cancelled"
                record["updated_at"] = utc_epoch()
                self._persist_job(record)
                return
            record["status"] = "running"
            record["attempts"] = int(record.get("attempts", 0)) + 1
            record["updated_at"] = utc_epoch()
            record["heartbeat_at"] = utc_epoch()
            self._persist_job(record)

        def heartbeat() -> None:
            record["heartbeat_at"] = utc_epoch()
            record["updated_at"] = record["heartbeat_at"]
            self._persist_job(record)

        try:
            result = self.run_task(record["task"], cancel_event=cancel_event, heartbeat=heartbeat)
            with self.lock:
                record["result"] = result
                record["status"] = "cancelled" if cancel_event.is_set() else result.get("status", "failed")
                record["updated_at"] = utc_epoch()
                self._persist_job(record)
            if record["status"] == "done" and record["task"].get("remember"):
                store = self.profile_store(record["task"].get("profile", "default"))
                store.add_memory(text=result.get("output", "")[:4000], kind="task_result", source_task_id=record["task"]["task_id"])
        except Exception as exc:
            with self.lock:
                record["status"] = "cancelled" if cancel_event.is_set() else "failed"
                record["result"] = {"protocol": record["task"]["protocol"], "task_id": record["task"]["task_id"], "error": str(exc)}
                record["updated_at"] = utc_epoch()
                self._persist_job(record)

    def get_job(self, job_id: str) -> dict[str, Any]:
        record = self.jobs.get(job_id)
        if not record:
            raise ProtocolError("job not found")
        return self._job_summary(record)

    def list_jobs(self) -> list[dict[str, Any]]:
        return [self._job_summary(record) for record in sorted(self.jobs.values(), key=lambda item: item.get("created_at", 0), reverse=True)[:100]]

    def list_goals(self) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in self.jobs.values():
            goal_id = record.get("goal_id")
            if goal_id:
                grouped.setdefault(goal_id, []).append(record)
        goals = []
        for goal_id, records in grouped.items():
            latest = max(records, key=lambda item: item.get("updated_at", 0))
            goals.append({"goal_id": goal_id, "status": latest.get("status"), "latest_job_id": latest.get("job_id"), "task_count": len(records), "updated_at": latest.get("updated_at")})
        return sorted(goals, key=lambda item: item.get("updated_at", 0), reverse=True)

    def resume_goal(self, goal_id: str) -> dict[str, Any]:
        records = [record for record in self.jobs.values() if record.get("goal_id") == goal_id]
        if not records:
            raise ProtocolError("goal not found")
        latest = max(records, key=lambda item: item.get("updated_at", 0))
        return self.resume_job(latest["job_id"])

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        record = self.jobs.get(job_id)
        if not record:
            raise ProtocolError("job not found")
        with self.lock:
            if record.get("status") in {"queued", "running"}:
                record["cancel_requested"] = True
                record["updated_at"] = utc_epoch()
                self._persist_job(record)
                event = self.job_cancel.get(job_id)
                if event:
                    event.set()
        return self._job_summary(record)

    def resume_job(self, job_id: str) -> dict[str, Any]:
        record = self.jobs.get(job_id)
        if not record:
            raise ProtocolError("job not found")
        with self.lock:
            if record.get("status") not in {"failed", "blocked", "cancelled", "interrupted"}:
                return self._job_summary(record)
            record["status"] = "queued"
            record["cancel_requested"] = False
            record["updated_at"] = utc_epoch()
            self._persist_job(record)
            self._start_job(job_id)
        return self._job_summary(record)

    def probe_capabilities(self, workspace: Path | None = None) -> dict[str, Any]:
        working_directory = workspace or self.server_root
        command = [sys.executable, "-B", str(self.client_script), "--working-directory", str(working_directory), "--capabilities-only", "--timeout-seconds", "20"]
        probe_env = os.environ.copy()
        if self.agents_json:
            probe_env["CLAUDE_A2A_AGENTS_JSON"] = self.agents_json
        process = subprocess.run(command, input="", text=True, encoding="utf-8", errors="replace", capture_output=True, cwd=str(working_directory), env=probe_env, timeout=30)
        try:
            receipt = json.loads(process.stdout.strip()) if process.stdout.strip() else {}
        except json.JSONDecodeError as exc:
            receipt = {"protocol_error": f"MCP capability probe returned malformed JSON: {exc}"}
        receipt["process_exit_code"] = process.returncode
        if process.stderr.strip():
            receipt["stderr"] = process.stderr.splitlines()[-8:]
        return receipt

    def run_task(self, task: dict[str, Any], *, cancel_event: threading.Event | None = None, heartbeat: Any = None) -> dict[str, Any]:
        workspace = resolve_workspace(self.server_root, task["workspace"]["path"])
        with self.lock:
            workspace_lock = self.workspace_locks.setdefault(str(workspace), threading.Lock())
        if not workspace_lock.acquire(blocking=False):
            raise ProtocolError("workspace is busy; concurrent task writers are rejected")
        try:
            return self._run_task_locked(task, workspace, cancel_event=cancel_event, heartbeat=heartbeat)
        finally:
            workspace_lock.release()

    def _run_task_locked(self, task: dict[str, Any], workspace: Path, *, cancel_event: threading.Event | None = None, heartbeat: Any = None) -> dict[str, Any]:
        if self.cli_fallback:
            return self._run_cli_fallback_locked(task, workspace, cancel_event=cancel_event, heartbeat=heartbeat)
        result = self._run_mcp_task_locked(task, workspace, cancel_event=cancel_event, heartbeat=heartbeat)
        receipt = result.get("server_receipt", {})
        native_error = str(receipt.get("protocol_error", "")).lower()
        safe_to_retry = (
            self.auto_cli_fallback
            and result.get("status") in {"blocked", "failed"}
            and is_native_capability_failure(native_error)
            # `worktree_changed` includes pre-existing user edits.  A native
            # capability failure is safe to fall back only when this attempt
            # changed no declared target path and did not move HEAD.
            and not result.get("changed_paths")
            and receipt.get("before_head") == receipt.get("after_head")
        )
        if not safe_to_retry:
            return result
        fallback = self._run_cli_fallback_locked(task, workspace, cancel_event=cancel_event, heartbeat=heartbeat)
        fallback_receipt = fallback.setdefault("server_receipt", {})
        fallback_receipt["native_attempt"] = receipt
        fallback["evidence"].insert(0, {
            "kind": "transport-fallback",
            "summary": "Native Claude capability was unavailable without changing the worktree; retried through the bounded Claude CLI adapter.",
            "command": "native MCP Agent -> claude.cmd --print",
            "exit_code": 0 if fallback.get("status") == "done" else 1,
        })
        return validate_result(fallback)

    def _run_mcp_task_locked(self, task: dict[str, Any], workspace: Path, *, cancel_event: threading.Event | None = None, heartbeat: Any = None) -> dict[str, Any]:
        target_paths = list(task["workspace"]["target_paths"])
        before = git_snapshot(workspace, target_paths)
        profile_context = self.profile_context(task)
        command = [sys.executable, "-B", str(self.client_script), "--working-directory", str(workspace)]
        team_mode = task["target_role"] == "team"
        agent_type = None
        if team_mode:
            members = []
            for member in task["team"]["members"]:
                configured_type = self.verifier_agent_type if member["role"] == "verifier" else self.worker_agent_type
                member_with_type = {**member}
                if configured_type:
                    member_with_type["agent_type"] = configured_type
                members.append(member_with_type)
            manifest = {
                "team_name": safe_team_name(task),
                "description": task["objective"][:500],
                "shared": {
                    "task_id": task["task_id"],
                    "context_digest": task["context_digest"],
                    "objective": task["objective"],
                    "target_paths": task["workspace"]["target_paths"],
                    "acceptance_criteria": task["acceptance_criteria"],
                    "constraints": task["constraints"],
                    "inputs": task["inputs"],
                    "profile_context": profile_context,
                },
                "members": members,
            }
            command += ["--team-mode"]
            command_input = json.dumps(manifest, ensure_ascii=False)
        else:
            agent_type = self.worker_agent_type if task["target_role"] == "worker" else self.verifier_agent_type
            if agent_type:
                command += ["--agent-type", agent_type]
            command_input = render_prompt(task, profile_context)
        if self.timeout_seconds is not None:
            command += ["--timeout-seconds", str(self.timeout_seconds)]
        child_env = os.environ.copy()
        if self.agents_json:
            child_env["CLAUDE_A2A_AGENTS_JSON"] = self.agents_json
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", cwd=str(workspace), env=child_env)
        communication: dict[str, str] = {"stdout": "", "stderr": ""}

        def communicate() -> None:
            stdout, stderr = process.communicate(command_input)
            communication["stdout"] = stdout
            communication["stderr"] = stderr

        communication_thread = threading.Thread(target=communicate, daemon=True)
        communication_thread.start()
        last_heartbeat = 0.0
        while communication_thread.is_alive():
            now = time.monotonic()
            if heartbeat and now - last_heartbeat >= 1:
                heartbeat()
                last_heartbeat = now
            if cancel_event and cancel_event.is_set() and process.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, text=True)
                else:
                    process.terminate()
            communication_thread.join(timeout=0.25)
        process.returncode = process.returncode if process.returncode is not None else 1
        mcp_receipt: dict[str, Any] = {}
        try:
            mcp_receipt = json.loads(communication["stdout"].strip()) if communication["stdout"].strip() else {}
        except json.JSONDecodeError as exc:
            mcp_receipt = {"protocol_error": f"MCP client returned malformed JSON: {exc}"}
        if cancel_event and cancel_event.is_set():
            mcp_receipt["protocol_error"] = "job cancellation requested"
            mcp_receipt["accepted_by_transport"] = False
        after = git_snapshot(workspace, target_paths)
        target_changed_paths = sorted(
            path for path in target_paths
            if before["target_fingerprints"].get(path) != after["target_fingerprints"].get(path)
        )
        new_paths = sorted(set(after["status_paths"]) - set(before["status_paths"]) | set(target_changed_paths))
        new_status_paths = sorted(set(after["status_paths"]) - set(before["status_paths"]))
        worktree_changed = before["change_fingerprint"] != after["change_fingerprint"]
        cleanup_ok = not mcp_receipt.get("cleanup_error")
        mcp_ok = bool(mcp_receipt.get("accepted_by_transport")) and process.returncode == 0 and cleanup_ok
        team_complete = not team_mode or bool(mcp_receipt.get("team_complete"))
        verifier_requested = task["target_role"] == "verifier" or any(member["role"] == "verifier" for member in task.get("team", {}).get("members", []))
        verifier_clean = not verifier_requested or (
            not target_changed_paths
            and not new_status_paths
            and before["head"] == after["head"]
        )
        expected_change = task.get("expected_change")
        scope_changed = bool(target_changed_paths)
        scope_unchanged = not scope_changed and not new_status_paths and before["head"] == after["head"]
        change_ok = expected_change is None or (scope_changed if expected_change else scope_unchanged)
        if not mcp_ok or not team_complete:
            status = "blocked" if is_native_capability_failure(mcp_receipt.get("protocol_error")) else "failed"
        elif not verifier_clean or not change_ok:
            status = "failed"
        else:
            status = "done"
        output = str(mcp_receipt.get("result_text", ""))[:MAX_TEXT_CHARS]
        if not output:
            output = str(mcp_receipt.get("protocol_error", "MCP client returned no result"))[:MAX_TEXT_CHARS]
        evidence = [
            {"kind": "mcp", "summary": f"MCP server {mcp_receipt.get('server_name')} {mcp_receipt.get('server_version')}; {mcp_receipt.get('tool_count')} tools; Agent available={mcp_receipt.get('agent_tool_available')}; native team={mcp_receipt.get('native_team_tools_available')}", "command": "claude.cmd mcp serve", "exit_code": process.returncode},
            {"kind": "worktree", "summary": f"HEAD unchanged={before['head'] == after['head']}; content changed={worktree_changed}; new status paths={new_paths}", "command": "git status --porcelain=v1 + diff/content fingerprints", "exit_code": 0},
        ]
        result = {
            "protocol": task["protocol"],
            "task_id": task["task_id"],
            "target_role": task["target_role"],
            "status": status,
            "output": output,
            "changed_paths": new_paths,
            "evidence": evidence,
            "context_digest": task["context_digest"],
            "server_receipt": {
                "transport": "mcp",
                "protocol_version": mcp_receipt.get("protocol_version"),
                "server_name": mcp_receipt.get("server_name"),
                "server_version": mcp_receipt.get("server_version"),
                "tool_count": mcp_receipt.get("tool_count"),
                "agent_tool_available": mcp_receipt.get("agent_tool_available"),
                "agent_type": agent_type,
                "team_mode": team_mode,
                "team_name": mcp_receipt.get("team_name"),
                "team_complete": team_complete,
                "native_team_tools_available": mcp_receipt.get("native_team_tools_available"),
                "native_team_mode": mcp_receipt.get("native_team_mode"),
                "legacy_team_tools_available": mcp_receipt.get("legacy_team_tools_available"),
                "protocol_error": mcp_receipt.get("protocol_error"),
                "cleanup_error": mcp_receipt.get("cleanup_error"),
                "accepted_by_transport": mcp_ok,
                "verifier_clean": verifier_clean,
                "change_expectation_satisfied": change_ok,
                "worktree_changed": worktree_changed,
                "before_head": before["head"],
                "after_head": after["head"],
            },
        }
        return validate_result(result)

    def _run_cli_delegate_once(
        self,
        workspace: Path,
        prompt: str,
        target_paths: list[str],
        *,
        allowed_tools: str,
        expected_change: bool | None,
        agent_type: str | None = None,
        cancel_event: threading.Event | None = None,
        heartbeat: Any = None,
    ) -> tuple[dict[str, Any], int]:
        """Run one fresh, bounded CLI adapter and return its receipt."""
        temp_root = Path(tempfile.mkdtemp(prefix="claude-a2a-cli-"))
        prompt_path = temp_root / "prompt.txt"
        result_path = temp_root / "receipt.json"
        prompt_path.write_text(prompt, encoding="utf-8")
        delegate = self.client_script.with_name("claude_delegate.ps1")
        command = [
            # The compatibility adapter is a bounded child process.  Bypass
            # the host's restrictive script policy for this exact invocation
            # rather than changing the user's or machine-wide policy.
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(delegate),
            "-WorkingDirectory", str(workspace),
            "-PromptFile", str(prompt_path),
            "-Transport", "Cli",
            "-AllowedTools", allowed_tools,
            "-ResultPath", str(result_path),
        ]
        if agent_type:
            command.extend(["-CliAgentType", agent_type])
        if target_paths:
            command.extend(["-TargetPathsJson", json.dumps(target_paths, ensure_ascii=False)])
        if expected_change is True:
            command.append("-ExpectChange")
        elif expected_change is False:
            command.append("-ExpectNoChange")
        if self.timeout_seconds is not None:
            command.extend(["-TimeoutSeconds", str(self.timeout_seconds)])

        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(workspace),
        )
        communication: dict[str, str] = {"stdout": "", "stderr": ""}
        started = time.monotonic()

        def force_stop() -> None:
            """Stop the CLI process without allowing cleanup to hang forever."""
            if process.poll() is not None:
                return
            if os.name == "nt":
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                except (OSError, subprocess.SubprocessError):
                    pass
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass

        heartbeat_stop = threading.Event()

        def heartbeat_loop() -> None:
            while not heartbeat_stop.is_set():
                if heartbeat:
                    heartbeat()
                heartbeat_stop.wait(1)

        heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
        heartbeat_thread.start()
        stop_requested = False
        try:
            while True:
                cancelled = bool(cancel_event and cancel_event.is_set())
                timed_out = self.timeout_seconds is not None and time.monotonic() - started > self.timeout_seconds + 15
                if cancelled or timed_out:
                    stop_requested = True
                    force_stop()
                    # A descendant can retain stdout/stderr after the parent
                    # has exited. Give communicate a short, bounded chance to
                    # drain them, then close our handles and return a timeout
                    # receipt instead of leaving the durable job running.
                    try:
                        stdout, stderr = process.communicate(timeout=2)
                        communication["stdout"] = stdout
                        communication["stderr"] = stderr
                    except subprocess.TimeoutExpired as exc:
                        communication["stdout"] = str(getattr(exc, "output", "") or "")
                        communication["stderr"] = str(getattr(exc, "stderr", "") or "")
                        for stream in (process.stdout, process.stderr):
                            if stream is not None:
                                try:
                                    stream.close()
                                except OSError:
                                    pass
                    break
                try:
                    stdout, stderr = process.communicate(timeout=1)
                    communication["stdout"] = stdout
                    communication["stderr"] = stderr
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)

        returncode = process.returncode if process.returncode is not None else 1
        receipt: dict[str, Any] = {}
        raw_receipt = communication["stdout"].strip()
        parse_error: str | None = None
        try:
            receipt = json.loads(raw_receipt) if raw_receipt else {}
        except json.JSONDecodeError as exc:
            parse_error = f"CLI delegate stdout returned malformed JSON: {exc}"
        # The PowerShell adapter writes the authoritative receipt to the
        # result path.  stdout may contain a control character from a nested
        # Claude report even when that file is valid JSON.
        if result_path.is_file():
            try:
                file_receipt = json.loads(result_path.read_text(encoding="utf-8"))
                if isinstance(file_receipt, dict):
                    receipt = file_receipt
            except (OSError, json.JSONDecodeError) as exc:
                if not receipt:
                    parse_error = f"CLI delegate receipt was malformed: {exc}"
        if parse_error:
            if receipt:
                receipt["stdout_parse_warning"] = parse_error
            else:
                receipt["json_parse_error"] = parse_error
        if cancel_event and cancel_event.is_set():
            receipt["accepted"] = False
            receipt["json_parse_error"] = "job cancellation requested"
        if stop_requested and not receipt.get("timed_out"):
            receipt["accepted"] = False
            receipt["timed_out"] = True
            receipt.setdefault("json_parse_error", "CLI delegate exceeded the bounded timeout or was cancelled")
        receipt.setdefault("stderr", communication["stderr"])
        shutil.rmtree(temp_root, ignore_errors=True)
        return receipt, returncode

    def _run_cli_fallback_locked(self, task: dict[str, Any], workspace: Path, *, cancel_event: threading.Event | None = None, heartbeat: Any = None) -> dict[str, Any]:
        """Run bounded CLI role(s) when native Agent Teams are unavailable.

        A worker/verifier team is represented by fresh CLI sessions in
        sequence, one for every declared worker and then one read-only
        verifier. This preserves disjoint worker scope even when native Agent
        Teams cannot be spawned, while making the receipt explicit that no
        native team ran.
        """
        before = git_snapshot(workspace, list(task["workspace"]["target_paths"]))
        profile_context = self.profile_context(task)
        target_paths = list(task["workspace"]["target_paths"])
        team_members = task.get("team", {}).get("members", []) if task["target_role"] == "team" else []
        worker_members = [member for member in team_members if member.get("role") == "worker"]
        verifier_member = next((member for member in team_members if member.get("role") == "verifier"), None)
        if task["target_role"] == "worker" and not worker_members:
            worker_members = [{"name": "worker", "role": "worker", "objective": task["objective"]}]
        run_worker = bool(worker_members)
        run_verifier = task["target_role"] == "verifier" or verifier_member is not None
        receipts: list[tuple[str, dict[str, Any], int]] = []
        verifier_isolated = False

        assigned_paths: set[str] = set()
        for index, worker_member in enumerate(worker_members):
            worker_objective = task["objective"]
            worker_objective += f"\n\nWorker member objective:\n{worker_member.get('objective', '')}"
            explicit_paths = worker_member.get("target_paths")
            if explicit_paths is None:
                explicit_paths = worker_member.get("workspace", {}).get("target_paths")
            if isinstance(explicit_paths, list):
                worker_paths = [path for path in explicit_paths if path in target_paths]
            else:
                searchable = json.dumps(worker_member, ensure_ascii=False)
                worker_paths = [
                    path for path in target_paths
                    if path in searchable or Path(path).name in searchable
                ]
            if not worker_paths:
                remaining_paths = [path for path in target_paths if path not in assigned_paths]
                worker_paths = remaining_paths[:1] if len(worker_members) > 1 and remaining_paths else target_paths
            assigned_paths.update(worker_paths)
            worker_workspace = {**task["workspace"], "target_paths": worker_paths}
            worker_task = {
                **task,
                "workspace": worker_workspace,
                "target_role": "worker",
                "objective": worker_objective,
            }
            receipt, returncode = self._run_cli_delegate_once(
                workspace,
                render_prompt(worker_task, profile_context),
                worker_paths,
                allowed_tools="Read,Edit,Write",
                expected_change=task.get("expected_change"),
                agent_type=self.worker_agent_type,
                cancel_event=cancel_event,
                heartbeat=heartbeat,
            )
            member_name = str(worker_member.get("name") or f"worker-{index + 1}")
            receipts.append((f"worker:{member_name}", receipt, returncode))

        worker_receipts = [receipt for role, receipt, _ in receipts if role.startswith("worker:")]
        worker_ok = bool(worker_receipts) and all(
            bool(receipt.get("accepted")) and returncode == 0
            for (role, receipt, returncode) in receipts
            if role.startswith("worker:")
        )
        if run_verifier and (not run_worker or worker_ok):
            verifier_objective = task["objective"]
            if verifier_member:
                verifier_objective += f"\n\nVerifier member objective:\n{verifier_member.get('objective', '')}"
            if worker_receipts:
                verifier_objective += "\n\nWorker receipts (inspect, do not trust blindly):\n" + json.dumps(worker_receipts, ensure_ascii=False)[:8000]
            verifier_task = {**task, "target_role": "verifier", "objective": verifier_objective}
            verifier_include_paths = list(target_paths)
            if run_verifier:
                # Bounded verifier prompts name their exact source files. Copy
                # only those paths instead of cloning a dirty novel checkout.
                # This applies to both direct verifier tasks and team tasks
                # whose fallback verifier runs after the worker sequence.
                # Keep the full-copy fallback for older packets without a
                # recognizable file list.
                input_paths = [
                    item["path"]
                    for item in task.get("inputs", [])
                    if isinstance(item, dict) and isinstance(item.get("path"), str)
                ]
                verifier_include_paths.extend(input_paths)
                objective_paths = re.findall(
                    r"(?<![A-Za-z0-9_./-])(?:seasons|docs|tools|engine)/[^\s,;`\"']+\.md",
                    str(task.get("objective", "")),
                )
                verifier_include_paths.extend(objective_paths)
            try:
                with isolated_cli_verifier_workspace(
                    workspace,
                    verifier_include_paths,
                    heartbeat=heartbeat,
                ) as verifier_workspace:
                    verifier_isolated = True
                    receipt, returncode = self._run_cli_delegate_once(
                        verifier_workspace,
                        render_prompt(verifier_task, profile_context),
                        target_paths,
                        # Verifiers need shell-level read-only evidence commands
                        # (git/grep/head/sha256sum). Edit/Write remain
                        # unavailable, and the post-run scope gate rejects any
                        # mutation in the disposable verifier workspace.
                        allowed_tools="Read,Glob,Grep,Bash",
                        expected_change=False,
                        agent_type=self.verifier_agent_type,
                        cancel_event=cancel_event,
                        heartbeat=heartbeat,
                    )
            except Exception as exc:
                receipt = {
                    "accepted": False,
                    "unexpected_worktree_change": False,
                    "branch_or_head_changed": False,
                    "json_parse_error": f"verifier isolation failed: {exc}",
                }
                returncode = 1
            receipts.append(("verifier", receipt, returncode))

        after = git_snapshot(workspace, target_paths)
        target_changed_paths = sorted(
            path for path in target_paths
            if before["target_fingerprints"].get(path) != after["target_fingerprints"].get(path)
        )
        new_paths = sorted(set(after["status_paths"]) - set(before["status_paths"]) | set(target_changed_paths))
        new_status_paths = sorted(set(after["status_paths"]) - set(before["status_paths"]))
        worktree_changed = before["change_fingerprint"] != after["change_fingerprint"]
        run_ok = all(bool(receipt.get("accepted")) and returncode == 0 for _, receipt, returncode in receipts)
        verifier_receipt = next((receipt for role, receipt, _ in receipts if role == "verifier"), None)
        verifier_clean = not run_verifier or bool(
            verifier_receipt
            and not verifier_receipt.get("unexpected_worktree_change")
            and not verifier_receipt.get("branch_or_head_changed")
        )
        expected_change = task.get("expected_change")
        scope_changed = bool(target_changed_paths)
        scope_unchanged = not scope_changed and not new_status_paths and before["head"] == after["head"]
        change_ok = expected_change is None or (scope_changed if expected_change else scope_unchanged)
        cli_ok = run_ok and verifier_clean and change_ok
        status = "done" if cli_ok else "failed"

        output_parts = []
        for role, receipt, _ in receipts:
            nested_stdout = receipt.get("stdout") if isinstance(receipt, dict) else None
            if not nested_stdout and isinstance(receipt, dict) and receipt.get("stdout_b64"):
                try:
                    nested_stdout = base64.b64decode(receipt["stdout_b64"], validate=True).decode("utf-8", errors="replace")
                except (ValueError, UnicodeError):
                    nested_stdout = ""
            result_text = ""
            if isinstance(nested_stdout, str) and nested_stdout.strip():
                try:
                    nested = json.loads(nested_stdout.strip())
                    result_text = str(nested.get("result", ""))
                except json.JSONDecodeError:
                    result_text = nested_stdout
            output_parts.append(f"[{role}] {result_text or receipt.get('json_parse_error') or receipt.get('stderr') or 'CLI delegate returned no result'}")
        output = "\n\n".join(output_parts) or "CLI fallback did not run a role"
        evidence = [
            {"kind": "cli-fallback", "summary": f"Claude CLI role receipts: {[(role, bool(receipt.get('accepted')), code) for role, receipt, code in receipts]}; native team not used.", "command": "claude.cmd --print", "exit_code": 0 if run_ok else 1},
            {"kind": "worktree", "summary": f"HEAD unchanged={before['head'] == after['head']}; content changed={worktree_changed}; new status paths={new_paths}", "command": "git status --porcelain=v1 + diff/content fingerprints", "exit_code": 0},
        ]
        result = {
            "protocol": task["protocol"],
            "task_id": task["task_id"],
            "target_role": task["target_role"],
            "status": status,
            "output": output[:MAX_TEXT_CHARS],
            "changed_paths": new_paths,
            "evidence": evidence,
            "context_digest": task["context_digest"],
            "server_receipt": {
                "transport": "cli-fallback",
                "team_mode": task["target_role"] == "team",
                "team_complete": (not task["target_role"] == "team") or cli_ok,
                "native_team_tools_available": False,
                "protocol_error": next((receipt.get("json_parse_error") for _, receipt, _ in receipts if receipt.get("json_parse_error")), None),
                "stdout_parse_warning": next((receipt.get("stdout_parse_warning") for _, receipt, _ in receipts if receipt.get("stdout_parse_warning")), None),
                "accepted_by_transport": run_ok,
                "verifier_clean": verifier_clean,
                "verifier_isolated": verifier_isolated,
                "change_expectation_satisfied": change_ok,
                "worktree_changed": worktree_changed,
                "before_head": before["head"],
                "after_head": after["head"],
            },
        }
        return validate_result(result)


class A2AHandler(BaseHTTPRequestHandler):
    server: "A2AServer"

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("claude-a2a: " + (format % args) + "\n")

    def send_json(self, status: int, value: Any) -> None:
        body = json_bytes(value)
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError as exc:
            if not is_client_disconnect(exc):
                raise

    def authorized(self) -> bool:
        token = self.server.state.auth_token
        if not token:
            return True
        presented = self.headers.get("Authorization", "")
        return hmac.compare_digest(presented, f"Bearer {token}")

    def read_json_body(self) -> Any:
        length = int(self.headers.get("Content-Length", "-1"))
        if length < 0 or length > MAX_PACKET_BYTES:
            raise ProtocolError(f"Content-Length must be between 0 and {MAX_PACKET_BYTES}")
        return json.loads(self.rfile.read(length))

    def do_GET(self) -> None:
        route = urlsplit(self.path).path.rstrip("/") or "/"
        if route == "/a2a/jobs" or route.startswith("/a2a/jobs/"):
            if not self.authorized():
                self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            try:
                if route == "/a2a/jobs":
                    self.send_json(HTTPStatus.OK, {"protocol": "claude-a2a/0.1", "jobs": self.server.state.list_jobs()})
                else:
                    self.send_json(HTTPStatus.OK, self.server.state.get_job(route.rsplit("/", 1)[-1]))
            except (ProtocolError, ValueError, OSError) as exc:
                self.send_json(HTTPStatus.NOT_FOUND, {"protocol": "claude-a2a/0.1", "error": str(exc)})
            return
        if route == "/a2a/goals":
            if not self.authorized():
                self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            self.send_json(HTTPStatus.OK, {"protocol": "claude-a2a/0.1", "goals": self.server.state.list_goals()})
            return
        if route == "/a2a/profiles":
            if not self.authorized():
                self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            self.send_json(HTTPStatus.OK, {"protocol": "claude-a2a/0.1", "profiles": self.server.state.list_profiles()})
            return
        if route == "/a2a/schedules":
            if not self.authorized():
                self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            self.send_json(HTTPStatus.OK, {"protocol": "claude-a2a/0.1", "schedules": self.server.state.list_schedules()})
            return
        if route == "/a2a/memory":
            if not self.authorized():
                self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            try:
                query = parse_qs(urlsplit(self.path).query)
                profile = query.get("profile", ["default"])[0]
                search = query.get("q", [None])[0]
                self.send_json(HTTPStatus.OK, {"protocol": "claude-a2a/0.1", "profile": profile, "memories": self.server.state.profile_store(profile).search_memory(search)})
            except (ProtocolError, StateError, ValueError, OSError) as exc:
                self.send_json(HTTPStatus.BAD_REQUEST, {"protocol": "claude-a2a/0.1", "error": str(exc)})
            return
        if self.path == "/capabilities":
            if not self.authorized():
                self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            try:
                receipt = self.server.state.probe_capabilities()
                healthy = bool(receipt.get("accepted_by_transport"))
                self.send_json(HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE, {
                    "protocol": "claude-a2a/0.1",
                    "healthy": healthy,
                    "relay": {"server": "claude-a2a", "workspace_root": str(self.server.state.server_root)},
                    "claude": receipt,
                })
            except Exception as exc:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"protocol": "claude-a2a/0.1", "healthy": False, "error": str(exc)})
            return
        if self.path != "/health":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        self.send_json(HTTPStatus.OK, {
            "protocol": "claude-a2a/0.1",
            "healthy": True,
            "server": "claude-a2a",
            "workspace_root": str(self.server.state.server_root),
            "lan_auth_required": bool(self.server.state.auth_token),
            "roles": ["orchestrator", "worker", "verifier", "team"],
            "team_target": True,
            "native_agent_teams": "probe at /capabilities",
            "cli_fallback": self.server.state.cli_fallback,
            "auto_cli_fallback": self.server.state.auto_cli_fallback,
            "fresh_mcp_session_per_task": True,
            "durable_jobs": bool(self.server.state.state_dir),
            "profiles_and_memory": bool(self.server.state.state_dir),
            "schedules": bool(self.server.state.state_dir),
            "state_dir": str(self.server.state.state_dir) if self.server.state.state_dir else None,
            "conversation_context_forwarded": False,
        })

    def do_POST(self) -> None:
        route = urlsplit(self.path).path.rstrip("/") or "/"
        if route in {"/a2a/jobs", "/a2a/tasks", "/a2a/memory", "/a2a/schedules"} or route.startswith("/a2a/jobs/") or route.startswith("/a2a/goals/") or route.startswith("/a2a/profiles/"):
            pass
        else:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        if not self.authorized():
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        try:
            if route == "/a2a/tasks":
                packet = validate_task(self.read_json_body())
                digest = packet["context_digest"]
                with self.server.state.lock:
                    previous = self.server.state.seen.get(packet["task_id"])
                    if previous and previous != digest:
                        raise ProtocolError("task_id was already used with a different context digest")
                    self.server.state.seen[packet["task_id"]] = digest
                result = self.server.state.run_task(packet)
                self.send_json(HTTPStatus.OK, result)
                return
            if route == "/a2a/jobs":
                packet = validate_task(self.read_json_body())
                self.send_json(HTTPStatus.ACCEPTED, self.server.state.enqueue_job(packet))
                return
            if route.endswith("/cancel") and route.startswith("/a2a/jobs/"):
                self.send_json(HTTPStatus.OK, self.server.state.cancel_job(route.split("/")[-2]))
                return
            if route.endswith("/resume") and route.startswith("/a2a/jobs/"):
                self.send_json(HTTPStatus.OK, self.server.state.resume_job(route.split("/")[-2]))
                return
            if route.endswith("/resume") and route.startswith("/a2a/goals/"):
                self.send_json(HTTPStatus.OK, self.server.state.resume_goal(route.split("/")[-2]))
                return
            if route == "/a2a/memory":
                body = self.read_json_body()
                if not isinstance(body, dict):
                    raise ProtocolError("memory body must be an object")
                profile = body.get("profile", "default")
                text = body.get("text")
                record = self.server.state.profile_store(profile).add_memory(text=text, kind=body.get("kind", "lesson"), tags=body.get("tags", []), source_task_id=body.get("source_task_id"))
                self.send_json(HTTPStatus.CREATED, {"protocol": "claude-a2a/0.1", "memory": record})
                return
            if route == "/a2a/schedules":
                self.send_json(HTTPStatus.CREATED, self.server.state.create_schedule(self.read_json_body()))
                return
            if route.startswith("/a2a/profiles/") and route.endswith("/skills"):
                profile = route.split("/")[3]
                body = self.read_json_body()
                if not isinstance(body, dict):
                    raise ProtocolError("skill body must be an object")
                result = self.server.state.profile_store(profile).write_skill(body.get("skill_ref", ""), body.get("content", ""))
                self.send_json(HTTPStatus.CREATED, {"protocol": "claude-a2a/0.1", "skill": result})
                return
            raise ProtocolError("not_found")
        except (ProtocolError, StateError, ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"protocol": "claude-a2a/0.1", "error": str(exc)})
        except Exception as exc:  # pragma: no cover - last-resort process boundary
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"protocol": "claude-a2a/0.1", "error": f"server_error: {exc}"})

    def do_DELETE(self) -> None:
        route = urlsplit(self.path).path.rstrip("/") or "/"
        if not self.authorized():
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        if route.startswith("/a2a/schedules/"):
            try:
                self.send_json(HTTPStatus.OK, self.server.state.cancel_schedule(route.rsplit("/", 1)[-1]))
            except (ProtocolError, ValueError, OSError) as exc:
                self.send_json(HTTPStatus.NOT_FOUND, {"protocol": "claude-a2a/0.1", "error": str(exc)})
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})


class A2AServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], state: A2AState) -> None:
        self.state = state
        super().__init__(address, A2AHandler)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the claude-a2a bounded A2A relay.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--workspace-root", default=os.getcwd())
    parser.add_argument("--auth-token")
    parser.add_argument("--worker-agent-type")
    parser.add_argument("--verifier-agent-type")
    parser.add_argument("--agents-json", default=os.environ.get("CLAUDE_A2A_AGENTS_JSON"))
    parser.add_argument("--cli-fallback", action="store_true", default=bool(os.environ.get("CLAUDE_A2A_CLI_FALLBACK")))
    parser.add_argument("--no-cli-fallback", action="store_true")
    parser.add_argument("--timeout-seconds", type=int)
    parser.add_argument("--state-dir", default=os.environ.get("CLAUDE_TEAM_BRIDGE_STATE_DIR"))
    args = parser.parse_args()
    if args.timeout_seconds is not None and args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be greater than zero")
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not (args.auth_token or os.environ.get("CLAUDE_A2A_AUTH_TOKEN")):
        parser.error("LAN binding requires --auth-token or CLAUDE_A2A_AUTH_TOKEN")
    if not args.state_dir:
        args.state_dir = str(Path.home() / ".claude-team-bridge")
    state = A2AState(args)
    with A2AServer((args.host, args.port), state) as server:
        print(json.dumps({"server": "claude-a2a", "protocol": "claude-a2a/0.1", "host": args.host, "port": args.port, "workspace_root": str(state.server_root)}, ensure_ascii=False), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
