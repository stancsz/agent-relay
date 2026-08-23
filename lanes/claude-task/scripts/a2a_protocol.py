"""Vendor-neutral, bounded task/result envelopes for claude-a2a."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

PROTOCOL = "claude-a2a/0.1"
ROLES = {"orchestrator", "worker", "verifier", "team"}
TARGET_ROLES = {"worker", "verifier", "team"}
STATUSES = {"done", "partial", "blocked", "failed"}
MAX_PACKET_BYTES = 200_000
MAX_OBJECTIVE_CHARS = 4_000
MAX_TEXT_CHARS = 12_000
MAX_PATCH_CHARS = 100_000
MAX_EXCERPT_CHARS = 12_000
MAX_ITEMS = 16
MAX_CRITERIA = 12
MAX_CONSTRAINTS = 16
MAX_TEAM_MEMBERS = 8
MAX_SKILL_REFS = 8
PATH_RE = re.compile(r"^(?![\\/])(?!(?:[.][.])(?:[\\/]|$))[^\x00]+$")
MEMBER_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
PROFILE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
SKILL_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
FORBIDDEN_CONTEXT_KEYS = {
    "conversation",
    "conversation_history",
    "context_window",
    "full_context",
    "messages",
    "prompt_history",
    "repository_dump",
    "session_context",
    "transcript",
}


class ProtocolError(ValueError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest_without_context_digest(packet: dict[str, Any]) -> str:
    unsigned = dict(packet)
    unsigned.pop("context_digest", None)
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def _reject_forbidden_keys(value: Any, path: str = "packet") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN_CONTEXT_KEYS:
                raise ProtocolError(f"{path}.{key} is forbidden; send bounded task inputs, not conversation context")
            _reject_forbidden_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden_keys(child, f"{path}[{index}]")


def _require_keys(value: dict[str, Any], required: set[str], allowed: set[str], label: str) -> None:
    missing = required - set(value)
    unknown = set(value) - allowed
    if missing:
        raise ProtocolError(f"{label} missing required keys: {sorted(missing)}")
    if unknown:
        raise ProtocolError(f"{label} has unknown keys: {sorted(unknown)}")


def _text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"{label} must be a non-empty string")
    if len(value) > maximum:
        raise ProtocolError(f"{label} exceeds {maximum} characters")
    return value


def _relative_path(value: Any, label: str) -> str:
    path = _text(value, label, 300).replace("\\", "/")
    if path.startswith("/") or ":" in path.split("/")[0] or "//" in path or not PATH_RE.match(path):
        raise ProtocolError(f"{label} must be a repository-relative path")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ProtocolError(f"{label} must not contain empty, dot, or parent path segments")
    return path


def _bounded_string_list(value: Any, label: str, maximum_items: int, maximum_chars: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise ProtocolError(f"{label} must be a list with at most {maximum_items} items")
    return [_text(item, f"{label}[{index}]", maximum_chars) for index, item in enumerate(value)]


def validate_task(packet: Any) -> dict[str, Any]:
    if not isinstance(packet, dict):
        raise ProtocolError("task must be a JSON object")
    if len(_canonical(packet)) > MAX_PACKET_BYTES:
        raise ProtocolError(f"task exceeds {MAX_PACKET_BYTES} bytes")
    _reject_forbidden_keys(packet)
    _require_keys(
        packet,
        {"protocol", "task_id", "caller_role", "target_role", "operation", "workspace", "objective", "acceptance_criteria", "constraints", "inputs", "context_digest"},
        {"protocol", "task_id", "caller_role", "target_role", "operation", "workspace", "objective", "acceptance_criteria", "constraints", "verification", "inputs", "team", "profile", "goal_id", "skill_refs", "memory_query", "remember", "expected_change", "context_digest"},
        "task",
    )
    if packet["protocol"] != PROTOCOL:
        raise ProtocolError(f"unsupported protocol: {packet['protocol']!r}")
    _text(packet["task_id"], "task_id", 120)
    if packet["caller_role"] not in ROLES or packet["target_role"] not in TARGET_ROLES:
        raise ProtocolError("caller_role or target_role is not an allowed role")
    if packet["caller_role"] != "orchestrator":
        raise ProtocolError("only orchestrator may submit a worker, verifier, or team task")
    if packet["operation"] not in {"work", "verify", "team"}:
        raise ProtocolError("operation must be 'work', 'verify', or 'team'")
    if packet["operation"] == "work" and packet["target_role"] != "worker":
        raise ProtocolError("work operation must target worker")
    if packet["operation"] == "verify" and packet["target_role"] != "verifier":
        raise ProtocolError("verify operation must target verifier")
    if packet["operation"] == "team" and packet["target_role"] != "team":
        raise ProtocolError("team operation must target team")
    if packet["target_role"] == "team":
        team = packet.get("team")
        if not isinstance(team, dict):
            raise ProtocolError("team must be an object for a team task")
        _require_keys(team, {"name", "members"}, {"name", "members"}, "team")
        team_name = _text(team["name"], "team.name", 60)
        if not MEMBER_NAME_RE.fullmatch(team_name):
            raise ProtocolError("team.name must contain only letters, numbers, underscores, or hyphens and start with a letter")
        members = team["members"]
        if not isinstance(members, list) or not 1 <= len(members) <= MAX_TEAM_MEMBERS:
            raise ProtocolError(f"team.members must contain between 1 and {MAX_TEAM_MEMBERS} members")
        names: set[str] = set()
        for index, member in enumerate(members):
            if not isinstance(member, dict):
                raise ProtocolError(f"team.members[{index}] must be an object")
            _require_keys(member, {"name", "role", "objective"}, {"name", "role", "objective", "acceptance_criteria", "constraints"}, f"team.members[{index}]")
            name = _text(member["name"], f"team.members[{index}].name", 32)
            if not MEMBER_NAME_RE.fullmatch(name) or name == "team-lead":
                raise ProtocolError(f"team.members[{index}].name is invalid or reserved")
            if name in names:
                raise ProtocolError(f"team member name is duplicated: {name}")
            names.add(name)
            if member["role"] not in {"worker", "verifier"}:
                raise ProtocolError(f"team.members[{index}].role must be worker or verifier")
            _text(member["objective"], f"team.members[{index}].objective", MAX_OBJECTIVE_CHARS)
            if "acceptance_criteria" in member:
                _bounded_string_list(member["acceptance_criteria"], f"team.members[{index}].acceptance_criteria", MAX_CRITERIA, MAX_TEXT_CHARS)
            if "constraints" in member:
                _bounded_string_list(member["constraints"], f"team.members[{index}].constraints", MAX_CONSTRAINTS, MAX_TEXT_CHARS)
    elif "team" in packet:
        raise ProtocolError("team is only allowed for a team task")
    profile = packet.get("profile", "default")
    if not isinstance(profile, str) or not PROFILE_RE.fullmatch(profile):
        raise ProtocolError("profile must be a safe profile identifier")
    if "goal_id" in packet and (not isinstance(packet["goal_id"], str) or not PROFILE_RE.fullmatch(packet["goal_id"])):
        raise ProtocolError("goal_id must be a safe goal identifier")
    skill_refs = packet.get("skill_refs", [])
    if not isinstance(skill_refs, list) or len(skill_refs) > MAX_SKILL_REFS:
        raise ProtocolError(f"skill_refs must contain at most {MAX_SKILL_REFS} items")
    for index, skill_ref in enumerate(skill_refs):
        if not isinstance(skill_ref, str) or not SKILL_REF_RE.fullmatch(skill_ref):
            raise ProtocolError(f"skill_refs[{index}] must be a safe skill identifier")
    if "memory_query" in packet:
        if not isinstance(packet["memory_query"], str) or len(packet["memory_query"]) > 500:
            raise ProtocolError("memory_query must be at most 500 characters")
    if "remember" in packet and not isinstance(packet["remember"], bool):
        raise ProtocolError("remember must be boolean")
    workspace = packet["workspace"]
    if not isinstance(workspace, dict):
        raise ProtocolError("workspace must be an object")
    _require_keys(workspace, {"path", "target_paths"}, {"path", "target_paths"}, "workspace")
    if workspace["path"] not in {".", ""}:
        _relative_path(workspace["path"], "workspace.path")
    target_paths = workspace["target_paths"]
    if not isinstance(target_paths, list) or len(target_paths) > MAX_ITEMS:
        raise ProtocolError(f"workspace.target_paths must contain at most {MAX_ITEMS} paths")
    for index, path in enumerate(target_paths):
        _relative_path(path, f"workspace.target_paths[{index}]")
    _text(packet["objective"], "objective", MAX_OBJECTIVE_CHARS)
    _bounded_string_list(packet["acceptance_criteria"], "acceptance_criteria", MAX_CRITERIA, MAX_TEXT_CHARS)
    _bounded_string_list(packet["constraints"], "constraints", MAX_CONSTRAINTS, MAX_TEXT_CHARS)
    if "verification" in packet:
        _bounded_string_list(packet["verification"], "verification", MAX_CRITERIA, MAX_TEXT_CHARS)
    inputs = packet["inputs"]
    if not isinstance(inputs, list) or len(inputs) > MAX_ITEMS:
        raise ProtocolError(f"inputs must contain at most {MAX_ITEMS} items")
    for index, item in enumerate(inputs):
        if not isinstance(item, dict):
            raise ProtocolError(f"inputs[{index}] must be an object")
        _require_keys(item, {"path", "sha256", "excerpt"}, {"path", "sha256", "excerpt"}, f"inputs[{index}]")
        _relative_path(item["path"], f"inputs[{index}].path")
        if not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-fA-F]{64}", item["sha256"]):
            raise ProtocolError(f"inputs[{index}].sha256 must be a SHA-256 hex digest")
        if not isinstance(item["excerpt"], str) or len(item["excerpt"]) > MAX_EXCERPT_CHARS:
            raise ProtocolError(f"inputs[{index}].excerpt must be at most {MAX_EXCERPT_CHARS} characters")
    if "expected_change" in packet and not isinstance(packet["expected_change"], bool):
        raise ProtocolError("expected_change must be boolean")
    if not isinstance(packet["context_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", packet["context_digest"]):
        raise ProtocolError("context_digest must be a lowercase SHA-256 digest")
    expected_digest = digest_without_context_digest(packet)
    if packet["context_digest"] != expected_digest:
        raise ProtocolError("context_digest does not match the bounded task packet")
    return packet


def validate_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ProtocolError("result must be a JSON object")
    if len(_canonical(result)) > MAX_PACKET_BYTES:
        raise ProtocolError(f"result exceeds {MAX_PACKET_BYTES} bytes")
    _reject_forbidden_keys(result)
    _require_keys(
        result,
        {"protocol", "task_id", "target_role", "status", "output", "changed_paths", "evidence", "context_digest"},
        {"protocol", "task_id", "target_role", "status", "output", "changed_paths", "evidence", "context_digest", "server_receipt", "patch"},
        "result",
    )
    if result["protocol"] != PROTOCOL or result["target_role"] not in TARGET_ROLES or result["status"] not in STATUSES:
        raise ProtocolError("result protocol, target_role, or status is invalid")
    _text(result["task_id"], "result.task_id", 120)
    if not isinstance(result["output"], str) or len(result["output"]) > MAX_TEXT_CHARS:
        raise ProtocolError(f"result.output must be at most {MAX_TEXT_CHARS} characters")
    paths = result["changed_paths"]
    if not isinstance(paths, list) or len(paths) > MAX_ITEMS:
        raise ProtocolError(f"result.changed_paths must contain at most {MAX_ITEMS} paths")
    for index, path in enumerate(paths):
        _relative_path(path, f"result.changed_paths[{index}]")
    if not isinstance(result["evidence"], list) or len(result["evidence"]) > MAX_ITEMS:
        raise ProtocolError(f"result.evidence must contain at most {MAX_ITEMS} items")
    for index, item in enumerate(result["evidence"]):
        if not isinstance(item, dict):
            raise ProtocolError(f"result.evidence[{index}] must be an object")
        if set(item) - {"kind", "summary", "command", "exit_code", "sha256"}:
            raise ProtocolError(f"result.evidence[{index}] contains unknown keys")
        _text(item.get("kind", ""), f"result.evidence[{index}].kind", 80)
        _text(item.get("summary", ""), f"result.evidence[{index}].summary", 2_000)
    if "patch" in result and (not isinstance(result["patch"], str) or len(result["patch"]) > MAX_PATCH_CHARS):
        raise ProtocolError(f"result.patch must be at most {MAX_PATCH_CHARS} characters")
    if not isinstance(result["context_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", result["context_digest"]):
        raise ProtocolError("result.context_digest must be a lowercase SHA-256 digest")
    return result


def build_task(*, task_id: str, target_role: str, operation: str, workspace_path: str = ".", target_paths: list[str] | None = None, objective: str, acceptance_criteria: list[str], constraints: list[str], inputs: list[dict[str, str]], team: dict[str, Any] | None = None, profile: str = "default", goal_id: str | None = None, skill_refs: list[str] | None = None, memory_query: str | None = None, remember: bool | None = None, expected_change: bool | None = None) -> dict[str, Any]:
    packet: dict[str, Any] = {
        "protocol": PROTOCOL,
        "task_id": task_id,
        "caller_role": "orchestrator",
        "target_role": target_role,
        "operation": operation,
        "workspace": {"path": workspace_path, "target_paths": target_paths or []},
        "objective": objective,
        "acceptance_criteria": acceptance_criteria,
        "constraints": constraints,
        "inputs": inputs,
        "profile": profile,
    }
    if team is not None:
        packet["team"] = team
    if goal_id is not None:
        packet["goal_id"] = goal_id
    if skill_refs:
        packet["skill_refs"] = skill_refs
    if memory_query is not None:
        packet["memory_query"] = memory_query
    if remember is not None:
        packet["remember"] = remember
    if expected_change is not None:
        packet["expected_change"] = expected_change
    packet["context_digest"] = digest_without_context_digest(packet)
    return validate_task(packet)
