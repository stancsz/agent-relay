"""Stdlib MCP client for Claude Code tools and native Agent Teams.

Single mode calls Agent once. Team mode capability-detects Claude Code's
modern implicit team sequence (Agent, TaskCreate/TaskUpdate, bounded
SendMessage) and the pre-v2.1.178 TeamCreate/TeamDelete sequence. No model,
base URL, credential, token, budget, or transcript override is passed.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from prompt_policy import with_high_agency_guidance


def drain(stream: Any, target: queue.Queue[str]) -> None:
    for line in stream:
        target.put(line.rstrip("\r\n"))


def response_text(response: dict[str, Any] | None) -> str:
    result = response.get("result") if response else None
    content = result.get("content", []) if isinstance(result, dict) else []
    return "\n".join(block.get("text", "") for block in content if block.get("type") == "text")


def response_is_error(response: dict[str, Any] | None) -> bool:
    result = response.get("result") if response else None
    return not isinstance(result, dict) or bool(result.get("isError")) or "error" in (response or {})


def json_text(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def safe_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")
    return name[:60] or "a2a-team"


def configured_agents_json() -> str | None:
    """Return the trusted host-level custom-agent policy, if configured."""
    raw = os.environ.get("CLAUDE_A2A_AGENTS_JSON", "").strip()
    if not raw:
        return None
    if len(raw) > 100_000:
        raise ValueError("CLAUDE_A2A_AGENTS_JSON exceeds 100000 characters")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"CLAUDE_A2A_AGENTS_JSON is not valid JSON: {exc}") from exc
    if not isinstance(value, dict) or not value:
        raise ValueError("CLAUDE_A2A_AGENTS_JSON must be a non-empty object")
    for name, definition in value.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise ValueError("CLAUDE_A2A_AGENTS_JSON contains an invalid agent name")
        if not isinstance(definition, dict):
            raise ValueError(f"CLAUDE_A2A_AGENTS_JSON agent {name!r} must be an object")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def claude_mcp_command(agents_json: str | None = None) -> list[str]:
    """Build a Windows-safe command for `claude.cmd mcp serve`."""
    batch_path = shutil.which("claude.cmd")
    executable: str | None = None
    if batch_path:
        candidate = Path(batch_path).resolve().parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if candidate.is_file():
            executable = str(candidate)
    if executable is None:
        executable = shutil.which("claude") or shutil.which("claude.cmd") or "claude.cmd"
    cli = [executable]
    if agents_json:
        cli.extend(["--agents", agents_json])
    cli.extend(["mcp", "serve"])
    if executable.lower().endswith((".cmd", ".bat")):
        # cmd.exe /c treats the rest as one command string.  Quote that
        # command explicitly so the JSON passed to --agents survives Windows
        # parsing when the direct Claude executable is unavailable.
        return ["cmd.exe", "/d", "/s", "/c", subprocess.list2cmdline(cli)]
    return cli


class McpSession:
    def __init__(self, working_directory: str, timeout_seconds: int | None, enable_teams: bool) -> None:
        env = os.environ.copy()
        if enable_teams:
            env["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "1"
        agents_json = configured_agents_json()
        self.agents_configured = bool(agents_json)
        self.process = subprocess.Popen(
            claude_mcp_command(agents_json),
            cwd=working_directory,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self.stdout_queue: queue.Queue[str] = queue.Queue()
        self.stderr_queue: queue.Queue[str] = queue.Queue()
        threading.Thread(target=drain, args=(self.process.stdout, self.stdout_queue), daemon=True).start()
        threading.Thread(target=drain, args=(self.process.stderr, self.stderr_queue), daemon=True).start()
        self.request_id = 0
        self.timeout_seconds = timeout_seconds
        self.started = time.monotonic()
        self.responses: list[dict[str, Any]] = []
        self.initialize: dict[str, Any] | None = None
        self.tools: list[dict[str, Any]] = []
        self.team_name: str | None = None
        self.team_file_path: Path | None = None
        self.spawned_names: list[str] = []
        self.team_complete = False
        self.native_team_mode: str | None = None

    def deadline(self) -> float | None:
        return None if self.timeout_seconds is None else self.started + self.timeout_seconds

    def send(self, message: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise RuntimeError("MCP server stdin is unavailable")
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def wait_for(self, response_id: int, deadline: float | None) -> dict[str, Any] | None:
        while deadline is None or time.monotonic() < deadline:
            try:
                line = self.stdout_queue.get(timeout=0.25)
            except queue.Empty:
                if self.process.poll() is not None:
                    break
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                item = {"raw": line}
            if isinstance(item, dict):
                self.responses.append(item)
                if item.get("id") == response_id:
                    return item
        return None

    def call(self, name: str, arguments: dict[str, Any], deadline: float | None = None) -> dict[str, Any] | None:
        self.request_id += 1
        request_id = self.request_id
        self.send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        return self.wait_for(request_id, self.deadline() if deadline is None else deadline)

    def initialize_and_list(self) -> str | None:
        self.request_id += 1
        self.send({
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "claude-a2a", "version": "0.4"},
            },
        })
        self.initialize = self.wait_for(self.request_id, self.deadline())
        if self.initialize is None:
            return "MCP initialize did not return a response."
        if "error" in self.initialize:
            return f"MCP initialize failed: {self.initialize['error']}"
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        self.request_id += 1
        self.send({"jsonrpc": "2.0", "id": self.request_id, "method": "tools/list", "params": {}})
        listing = self.wait_for(self.request_id, self.deadline())
        if listing is None:
            return "MCP tools/list did not return a response."
        if "error" in listing:
            return f"MCP tools/list failed: {listing['error']}"
        self.tools = listing.get("result", {}).get("tools", [])
        return None

    def shutdown(self) -> str | None:
        if not self.team_name:
            return None
        errors: list[str] = []
        for name in self.spawned_names:
            response = self.call("SendMessage", {"to": name, "message": {"type": "shutdown_request", "reason": "A2A task complete; clean up the native team."}})
            if response_is_error(response):
                errors.append(f"shutdown {name}: {response_text(response) or 'error'}")
        # Claude acknowledges shutdown asynchronously. Waiting for those
        # acknowledgements prevents a teammate's final inbox write from
        # recreating the directory immediately after TeamDelete.
        if self.native_team_mode == "legacy-create-delete" and self.team_file_path:
            canonical_team_dir = (Path.home() / ".claude" / "teams" / (self.team_name or "")).resolve()
            actual_team_dir = self.team_file_path.resolve().parent
            inbox = actual_team_dir / "inboxes" / "team-lead.json"
            if actual_team_dir != canonical_team_dir:
                pending = set()
            else:
                pending = set(self.spawned_names)
            ack_deadline = time.monotonic() + 5
            while pending and time.monotonic() < ack_deadline:
                try:
                    messages = json.loads(inbox.read_text(encoding="utf-8")) if inbox.is_file() else []
                except (OSError, json.JSONDecodeError):
                    messages = []
                for message in messages if isinstance(messages, list) else []:
                    sender = message.get("from")
                    event = json_text(message.get("text", "")) if isinstance(message.get("text"), str) else {}
                    if sender in pending and event.get("type") == "shutdown_response":
                        pending.discard(sender)
                if pending:
                    time.sleep(0.1)
        if self.native_team_mode == "implicit-agent":
            # Modern Claude Code cleans up the team config when this session
            # exits; there is intentionally no TeamDelete call in this mode.
            return "; ".join(errors) if errors else None
        cleanup_deadline = time.monotonic() + 30
        while True:
            response = self.call("TeamDelete", {}, deadline=cleanup_deadline)
            if not response_is_error(response):
                artifact_error = self.remove_ephemeral_artifacts()
                if artifact_error:
                    errors.append(artifact_error)
                return "; ".join(errors) if errors else None
            error_text = response_text(response) or "TeamDelete failed"
            if time.monotonic() >= cleanup_deadline:
                errors.append(error_text)
                return "; ".join(errors)
            time.sleep(1)

    def remove_ephemeral_artifacts(self) -> str | None:
        """Remove only this bridge's exact native team/task directories.

        Some Claude Code versions leave the already-deleted team's JSON state
        on disk. The paths are deleted only after TeamDelete succeeds, and
        only when the team name and both parents match the user's Claude home
        directories. Failed native cleanup deliberately keeps artifacts.
        """
        if self.native_team_mode != "legacy-create-delete" or not self.team_name or not self.team_name.startswith("a2a-") or not self.team_file_path:
            return None
        claude_home = (Path.home() / ".claude").resolve()
        expected_team_dir = claude_home / "teams" / self.team_name
        actual_team_dir = self.team_file_path.resolve().parent
        if actual_team_dir != expected_team_dir or actual_team_dir.parent != (claude_home / "teams"):
            # Test doubles and older Claude builds may report a disposable
            # fixture path. Never delete outside the canonical Claude home.
            return None
        expected_task_dir = claude_home / "tasks" / self.team_name
        cleanup_deadline = time.monotonic() + 5
        while True:
            try:
                if actual_team_dir.is_dir():
                    shutil.rmtree(actual_team_dir)
                if expected_task_dir.is_dir():
                    shutil.rmtree(expected_task_dir)
            except OSError as exc:
                if time.monotonic() >= cleanup_deadline:
                    return f"ephemeral artifact cleanup failed: {exc}"
            if not actual_team_dir.exists() and not expected_task_dir.exists():
                return None
            if time.monotonic() >= cleanup_deadline:
                return "ephemeral artifact cleanup did not settle after TeamDelete"
            time.sleep(0.1)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)


def slim_receipt(session: McpSession) -> dict[str, Any]:
    server_info = session.initialize.get("result", {}) if session.initialize else {}
    available = {tool.get("name") for tool in session.tools}
    modern_required = {"Agent", "TaskCreate", "TaskUpdate", "TaskList", "SendMessage"}
    legacy_required = modern_required | {"TeamCreate", "TeamDelete"}
    return {
        "protocol_version": server_info.get("protocolVersion"),
        "server_name": server_info.get("serverInfo", {}).get("name"),
        "server_version": server_info.get("serverInfo", {}).get("version"),
        "tool_count": len(session.tools),
        "tool_names": [tool.get("name") for tool in session.tools],
        "agent_tool_available": any(tool.get("name") == "Agent" for tool in session.tools),
        "agents_configured": session.agents_configured,
        "native_team_tools_available": modern_required <= available,
        "native_team_mode": "legacy-create-delete" if legacy_required <= available else ("implicit-agent" if modern_required <= available else None),
        "legacy_team_tools_available": legacy_required <= available,
        "team_file_path": str(session.team_file_path) if session.team_file_path else None,
    }


def render_member_prompt(manifest: dict[str, Any], member: dict[str, Any]) -> str:
    shared = manifest["shared"]
    collaboration_lines: list[str] = []
    collaboration = shared.get("collaboration")
    if isinstance(collaboration, dict):
        collaboration_lines = [
            "",
            "## Parent collaboration contract",
            f"Mode: {collaboration['mode']}",
            f"Context policy: {collaboration['context_policy']}",
            "Before editing:",
            *[f"- {item}" for item in collaboration["before_edit"]],
            f"Question policy: {collaboration['question_policy']}",
            "Required parent handoff fields:",
            *[f"- {field}" for field in collaboration["handoff_fields"]],
            "If remote facts are missing, send the exact question to team-lead rather than guessing.",
        ]
    progress_lines: list[str] = []
    progress = shared.get("progress_contract")
    if isinstance(progress, dict):
        progress_lines = [
            "",
            "## Required babystep evidence",
            f"Format: {progress['evidence_format']}",
            *[
                f"Emit exactly one `PROGRESS {step} | status=... | evidence=...` line before handoff; use not_applicable only with a concrete explanation."
                for step in progress["steps"]
            ],
            "Missing, vague, or blocked evidence fails the parent receipt closed.",
        ]
    return with_high_agency_guidance("\n".join([
        "## Claude A2A native team member task",
        f"Team: {manifest['team_name']}",
        f"Member: {member['name']}",
        f"Role: {member['role']}",
        "You have an independent context window. Do not ask for or infer the lead's conversation history.",
        "Only use this bounded task packet and the repository context Claude loaded for this session.",
        "You may coordinate with teammates only through short SendMessage messages; do not send transcripts, large file dumps, credentials, or approval claims.",
        "",
        "## Shared objective",
        shared["objective"],
        "",
        "## Your objective",
        member["objective"],
        "",
        f"Context digest: {shared['context_digest']}",
        "",
        "## Target paths",
        *[f"- {path}" for path in shared["target_paths"]],
        "",
        "## Bounded inputs",
        *[f"- {item['path']} (sha256 {item['sha256']}):\n{item['excerpt']}" for item in shared.get("inputs", [])],
        *collaboration_lines,
        *progress_lines,
        "",
        "## Bounded profile context",
        *( [f"- Skill {skill_ref}:\n{content}" for skill_ref, content in shared.get("profile_context", {}).get("skills", {}).items()] or ["- No reusable skills supplied."] ),
        *( [f"- Memory ({memory.get('kind', 'lesson')}):\n{memory.get('text', '')}" for memory in shared.get("profile_context", {}).get("memories", [])] or ["- No profile memories supplied."] ),
        "",
        "## Acceptance criteria",
        *[f"- {criterion}" for criterion in member.get("acceptance_criteria", shared["acceptance_criteria"])],
        "",
        "## Constraints",
        *[f"- {constraint}" for constraint in member.get("constraints", shared["constraints"])],
        "",
        "When finished, send exactly one plain-text message to team-lead beginning with `A2A_RESULT `, followed by one progress line for every required step, then the required parent handoff fields, actual work, commands and exit codes, files changed, risks, and unmet criteria. Then remain idle. Do not claim checks you did not run.",
    ]))


def run_team(session: McpSession, manifest: dict[str, Any]) -> tuple[str, str | None, list[dict[str, Any]]]:
    required = {"Agent", "TaskCreate", "TaskUpdate", "TaskList", "SendMessage"}
    available = {tool.get("name") for tool in session.tools}
    missing = sorted(required - available)
    if missing:
        return "", f"native Agent Teams are unavailable; missing MCP tools: {', '.join(missing)}", []
    legacy = {"TeamCreate", "TeamDelete"} <= available
    session.native_team_mode = "legacy-create-delete" if legacy else "implicit-agent"
    previous_team_dirs = team_dirs()
    if legacy:
        team_response = session.call("TeamCreate", {"team_name": manifest["team_name"], "description": manifest.get("description", "Claude A2A native team")})
        if response_is_error(team_response):
            return "", response_text(team_response) or "TeamCreate failed", []
        team_info = json_text(response_text(team_response))
        session.team_name = manifest["team_name"]
        file_path = team_info.get("team_file_path")
        if file_path:
            session.team_file_path = Path(file_path)
        if session.team_file_path is None:
            return "", "TeamCreate did not return team_file_path; refusing to read unbounded team state", []
    spawned: list[dict[str, Any]] = []
    for member in manifest["members"]:
        arguments: dict[str, Any] = {
            "description": f"{member['role']} {member['name']}",
            "prompt": render_member_prompt(manifest, member),
            "name": member["name"],
        }
        if session.team_name:
            arguments["team_name"] = session.team_name
        if member.get("agent_type"):
            arguments["subagent_type"] = member["agent_type"]
        response = session.call("Agent", arguments)
        if response_is_error(response):
            return "", f"failed to spawn {member['name']}: {response_text(response) or 'Agent failed'}", spawned
        if session.native_team_mode == "implicit-agent" and session.team_file_path is None:
            team_info = json_text(response_text(response))
            hinted_name = team_info.get("team_name")
            if not hinted_name:
                for key in ("teammate_id", "agent_id"):
                    value = team_info.get(key)
                    if isinstance(value, str) and "@" in value:
                        hinted_name = value.rsplit("@", 1)[-1]
                        break
            discover_team_state(session, previous_team_dirs, hinted_name)
            if session.team_file_path is None:
                return "", "modern Agent Teams spawned a teammate but no team mailbox became discoverable", spawned
        session.spawned_names.append(member["name"])
        spawned.append({"name": member["name"], "role": member["role"], "spawn": json_text(response_text(response))})
    task_ids: dict[str, str] = {}
    for member in manifest["members"]:
        task_response = session.call("TaskCreate", {"subject": f"{member['role']}: {member['name']}", "description": member["objective"]})
        if response_is_error(task_response):
            return "", f"failed to create task for {member['name']}: {response_text(task_response) or 'TaskCreate failed'}", spawned
        task_info = json_text(response_text(task_response))
        task = task_info.get("task") if isinstance(task_info.get("task"), dict) else task_info
        task_id = str(task.get("id", "")) if isinstance(task, dict) else ""
        if task_id:
            task_ids[member["name"]] = task_id
            session.call("TaskUpdate", {"taskId": task_id, "owner": member["name"], "status": "in_progress"})
    results: dict[str, str] = {}
    failed_members: dict[str, str] = {}
    idle_members: set[str] = set()
    reminded_members: set[str] = set()
    member_names = {member["name"] for member in manifest["members"]}
    inbox = session.team_file_path.parent / "inboxes" / "team-lead.json" if session.team_file_path else None
    deadline = session.deadline()
    while len(results) < len(manifest["members"]):
        if inbox and inbox.is_file():
            try:
                messages = json.loads(inbox.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                messages = []
            for message in messages if isinstance(messages, list) else []:
                sender = message.get("from")
                text = message.get("text", "")
                if sender in member_names and isinstance(text, str) and text.startswith("A2A_RESULT "):
                    results[sender] = text[:12_000]
                elif sender in member_names and isinstance(text, str):
                    event = json_text(text)
                    if sender not in results and event.get("type") == "idle_notification":
                        idle_members.add(sender)
                    elif sender not in results and event.get("type") == "teammate_failed":
                        failed_members[sender] = "teammate_failed"
        for sender in sorted(idle_members - set(results) - reminded_members):
            reminder = session.call("SendMessage", {"to": sender, "message": "You are idle without an A2A_RESULT. Send exactly one plain-text A2A_RESULT message to team-lead now, then remain idle.", "summary": "A2A result reminder"})
            if response_is_error(reminder):
                failed_members[sender] = response_text(reminder) or "could not remind idle team member"
            reminded_members.add(sender)
        if failed_members:
            return "\n\n".join(results.values()), "native team member stopped without an A2A_RESULT: " + ", ".join(f"{name} ({reason})" for name, reason in sorted(failed_members.items())), spawned
        if len(results) == len(manifest["members"]):
            break
        if deadline is not None and time.monotonic() >= deadline:
            return "\n\n".join(results.values()), "timed out waiting for native team result messages", spawned
        time.sleep(0.5)
    for member in manifest["members"]:
        task_id = task_ids.get(member["name"])
        if task_id:
            session.call("TaskUpdate", {"taskId": task_id, "status": "completed"})
    session.team_complete = True
    return "\n\n".join(results[member["name"]] for member in manifest["members"]), None, spawned


def team_dirs() -> set[Path]:
    root = team_state_root()
    try:
        return {path.resolve() for path in root.iterdir() if path.is_dir()}
    except OSError:
        return set()


def discover_team_state(session: McpSession, previous_dirs: set[Path], hinted_name: str | None) -> None:
    root = team_state_root()
    candidates: list[Path] = []
    if isinstance(hinted_name, str) and hinted_name:
        hinted_path = root / hinted_name
        if hinted_path.is_dir():
            candidates.append(hinted_path)
    candidates.extend(sorted(team_dirs() - previous_dirs, key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True))
    # A modern session may create its team directory just before the first
    # Agent response, so also accept the newest config written after start.
    if not candidates:
        for path in team_dirs():
            config = path / "config.json"
            try:
                if config.is_file() and config.stat().st_mtime >= session.started - 2:
                    candidates.append(path)
            except OSError:
                continue
    for candidate in candidates:
        config = candidate / "config.json"
        inbox = candidate / "inboxes" / "team-lead.json"
        if config.is_file() and inbox.parent.is_dir():
            session.team_file_path = config
            session.team_name = candidate.name
            return


def team_state_root() -> Path:
    """Return the Claude team-state root; tests may point it at an isolated fixture."""
    configured = os.environ.get("CLAUDE_TEAM_STATE_ROOT")
    return Path(configured).resolve() if configured else (Path.home() / ".claude" / "teams").resolve()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--working-directory", required=True)
    parser.add_argument("--agent-type")
    parser.add_argument("--timeout-seconds", type=int)
    parser.add_argument("--health-only", action="store_true")
    parser.add_argument("--capabilities-only", action="store_true")
    parser.add_argument("--team-mode", action="store_true")
    args = parser.parse_args()
    raw_input = sys.stdin.read()
    manifest: dict[str, Any] | None = None
    prompt = raw_input
    if args.team_mode:
        try:
            manifest = json.loads(raw_input)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"team manifest must be JSON: {exc}")
        if not isinstance(manifest, dict) or not manifest.get("members"):
            raise SystemExit("team manifest must include members")
        prompt = ""
    elif args.health_only and not prompt.strip():
        prompt = "Do not read or edit files. Return exactly MCP_HEALTH_OK and nothing else."
    if not args.capabilities_only and not args.team_mode and not prompt.strip():
        raise SystemExit("MCP delegation prompt must not be empty.")
    if args.timeout_seconds is not None and args.timeout_seconds <= 0:
        raise SystemExit("timeout-seconds must be greater than zero when supplied.")
    # The capability probe is specifically for the native team path, so it
    # enables Claude Code's experimental Agent Teams flag without changing
    # model, endpoint, credentials, or budgets.
    session = McpSession(args.working_directory, args.timeout_seconds, args.team_mode or args.capabilities_only)
    protocol_error = session.initialize_and_list()
    result_text = ""
    agent_response: dict[str, Any] | None = None
    team_results: list[dict[str, Any]] = []
    cleanup_error = None
    try:
        if not protocol_error and args.capabilities_only:
            pass
        elif not protocol_error and args.team_mode and manifest is not None:
            result_text, protocol_error, team_results = run_team(session, manifest)
        elif not protocol_error:
            if not any(tool.get("name") == "Agent" for tool in session.tools):
                protocol_error = "Claude MCP server does not expose the Agent tool."
            else:
                arguments: dict[str, Any] = {"description": "Delegated repository task", "prompt": prompt}
                if args.agent_type:
                    arguments["subagent_type"] = args.agent_type
                agent_response = session.call("Agent", arguments)
                result_text = response_text(agent_response)
                if response_is_error(agent_response):
                    protocol_error = result_text or "MCP Agent tool returned an error."
    finally:
        if args.team_mode:
            cleanup_error = session.shutdown()
        session.close()
    if args.capabilities_only:
        success = protocol_error is None
    else:
        success = bool(not protocol_error and result_text.strip())
    receipt = {
        "transport": "mcp",
        **slim_receipt(session),
        "agent_type": args.agent_type,
        "health_only": args.health_only,
        "capabilities_only": args.capabilities_only,
        "team_mode": args.team_mode,
        "team_name": manifest.get("team_name") if manifest else None,
        "team_results": team_results,
        "team_complete": session.team_complete,
        "agent_response": agent_response,
        "result_text": result_text,
        "protocol_error": protocol_error,
        "cleanup_error": cleanup_error,
        "timed_out": bool(args.timeout_seconds is not None and session.deadline() is not None and time.monotonic() >= session.deadline()),
        "server_process_exit_code": session.process.returncode,
        "duration_ms": round((time.monotonic() - session.started) * 1000),
        "stderr": list(session.stderr_queue.queue),
        "accepted_by_transport": success,
    }
    print(json.dumps(receipt, ensure_ascii=False))
    return 0 if success else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"transport": "mcp", "protocol_error": f"MCP client failed: {exc}", "accepted_by_transport": False}, ensure_ascii=False))
        raise SystemExit(1)
