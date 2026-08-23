"""Canonical subagent lane registry.

The registry is deliberately descriptive.  It keeps routing names and their
authority boundaries in one place without pretending that every lane shares a
transport or a model runtime.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import shutil
from typing import Callable
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from .env import load_dotenv


@dataclass(frozen=True)
class SubagentLane:
    name: str
    role: str
    execution: str
    model: str
    reasoning: str | None
    mutates_worktree: bool
    verification: str
    status: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


LANES: tuple[SubagentLane, ...] = (
    SubagentLane(
        name="local-qwen",
        role="mechanical worker",
        execution="Codex CLI over Ollama",
        model="qwen3.5:4b",
        reasoning="low",
        mutates_worktree=False,
        verification="candidate diff, scope gate, declared checks, parent rerun",
    ),
    SubagentLane(
        name="claude-task",
        role="Claude implementation worker",
        execution="Authenticated Claude Code task bridge with optional Agent Teams",
        model="host policy",
        reasoning=None,
        mutates_worktree=True,
        verification="bounded task receipt, Git/workspace gates, parent tests",
    ),
    SubagentLane(
        name="claude-mcp",
        role="remote Claude MCP worker",
        execution="Existing Claude streamable-HTTP MCP server",
        model="host policy",
        reasoning=None,
        mutates_worktree=True,
        verification="remote MCP output and transport receipt; no local sandbox claim",
    ),
    SubagentLane(
        name="sol-reviewer",
        role="Sol high independent read-only reviewer",
        execution="Codex CLI subscription via logged-in credentials",
        model="gpt-5.6-sol",
        reasoning="high",
        mutates_worktree=False,
        verification="read-only review receipt and independent findings",
    ),
    SubagentLane(
        name="agy-antigravity",
        role="Google-stack scout/planner",
        execution="Google Antigravity CLI",
        model="gemini-3.1-pro-high",
        reasoning="high",
        mutates_worktree=False,
        verification="plan receipt and evidence; parent owns implementation and tests",
    ),
)

# Keep legacy spellings accepted for existing task packets; manifests and new
# documentation expose only the canonical role names above.
_ALIASES = {
    "ollama": "local-qwen",
    "codex-ollama": "local-qwen",
    "qwen": "local-qwen",
    "claude": "claude-task",
    "claude-team": "claude-task",
    "review": "sol-reviewer",
    "codex-review": "sol-reviewer",
    "codex-verifier": "sol-reviewer",
    "sol": "sol-reviewer",
    "sol-high": "sol-reviewer",
    "agy": "agy-antigravity",
    "antigravity": "agy-antigravity",
}


def canonical_lane_name(value: str) -> str:
    candidate = value.strip().lower()
    candidate = _ALIASES.get(candidate, candidate)
    if candidate not in {lane.name for lane in LANES}:
        allowed = ", ".join(lane.name for lane in LANES)
        raise ValueError(f"unknown subagent lane {value!r}; choose one of {allowed}")
    return candidate


def _lane_with_env_overrides(lane: SubagentLane) -> SubagentLane:
    if lane.name == "local-qwen":
        model = os.environ.get("AR_CODEX_MODEL", lane.model)
    elif lane.name == "sol-reviewer":
        model = os.environ.get("AR_CODEX_REVIEW_MODEL", lane.model)
    elif lane.name == "agy-antigravity":
        model = os.environ.get("AR_AGY_MODEL", lane.model)
    else:
        model = lane.model
    if model == lane.model:
        return lane
    return SubagentLane(
        **{
            **lane.to_dict(),
            "model": model,
        }
    )


def lane_manifest() -> list[dict[str, Any]]:
    load_dotenv()
    return [lane.to_dict() for lane in (_lane_with_env_overrides(lane) for lane in LANES)]


def _executable_from_env(*names: str, defaults: tuple[str, ...] = ()) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    for value in defaults:
        resolved = shutil.which(value)
        if resolved:
            return resolved
    return None


def _claude_bridge_script() -> Path:
    selected = os.environ.get("AR_CLAUDE_BRIDGE_SCRIPT")
    if selected:
        return Path(selected).expanduser().resolve()
    return (
        Path(__file__).resolve().parents[2]
        / "lanes"
        / "claude-task"
        / "scripts"
        / "claude_a2a_server.py"
    )


def _claude_health(*, probe_endpoint: bool = True) -> dict[str, Any]:
    executable = _executable_from_env(
        "AR_CLAUDE_BIN", defaults=("claude", "claude.cmd", "claude.exe")
    )
    bridge_script = _claude_bridge_script()
    missing: list[str] = []
    if not executable:
        missing.append("Claude CLI")
    if not bridge_script.is_file():
        missing.append(f"bridge script {bridge_script}")
    if missing:
        return {
            "status": "blocked",
            "healthy": False,
            "transport": "claude-a2a-ephemeral",
            "executable": executable,
            "bridge_script": str(bridge_script),
            "error": "missing " + "; ".join(missing),
        }

    configured_url = os.environ.get("AR_CLAUDE_A2A_SERVER_URL") or os.environ.get(
        "CLAUDE_A2A_SERVER_URL"
    )
    if not probe_endpoint or not configured_url:
        return {
            "status": "unknown",
            "healthy": None,
            "transport": "claude-a2a-ephemeral",
            "executable": executable,
            "bridge_script": str(bridge_script),
            "probe": "executable-and-bridge-script",
        }
    url = configured_url.rstrip("/")
    request = Request(f"{url}/health", headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=1.5) as response:
            import json

            payload = json.loads(response.read().decode("utf-8"))
        healthy = bool(payload.get("healthy"))
        return {
            "status": "ready" if healthy else "degraded",
            "endpoint": url,
            "healthy": healthy,
            "server": payload.get("server"),
            "transport": "claude-a2a-configured-endpoint",
            "executable": executable,
            "bridge_script": str(bridge_script),
        }
    except (OSError, URLError, ValueError) as exc:
        return {
            "status": "blocked",
            "endpoint": url,
            "healthy": False,
            "transport": "claude-a2a-configured-endpoint",
            "executable": executable,
            "bridge_script": str(bridge_script),
            "error": str(exc),
        }


def _claude_mcp_health(*, probe_endpoint: bool = True) -> dict[str, Any]:
    configured_url = os.environ.get("AR_CLAUDE_MCP_URL") or os.environ.get("CLAUDE_MCP_URL")
    if not configured_url:
        return {
            "status": "unknown",
            "healthy": None,
            "transport": "streamable-http-mcp",
            "probe": "AR_CLAUDE_MCP_URL not configured",
        }
    endpoint = configured_url.rstrip("/")
    base = {
        "endpoint": endpoint,
        "transport": "streamable-http-mcp",
    }
    if not probe_endpoint:
        return {"status": "unknown", "healthy": None, **base, "probe": "configured endpoint only"}
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-03-26",
    }
    token = os.environ.get("AR_CLAUDE_MCP_AUTH_TOKEN") or os.environ.get("CLAUDE_MCP_AUTH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "agent-relay-health", "version": "0.1.0"},
            },
        }
    ).encode("utf-8")
    try:
        with urlopen(Request(endpoint, method="POST", data=body, headers=headers), timeout=2.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
            session_id = response.headers.get("MCP-Session-Id")
        if session_id:
            close_headers = dict(headers)
            close_headers["MCP-Session-Id"] = session_id
            try:
                with urlopen(Request(endpoint, method="DELETE", headers=close_headers), timeout=1.0):
                    pass
            except OSError:
                pass
        healthy = isinstance(payload, dict) and isinstance(payload.get("result"), dict)
        return {
            "status": "ready" if healthy else "degraded",
            "healthy": healthy,
            **base,
            "server": (payload.get("result", {}).get("serverInfo", {}) if isinstance(payload, dict) else {}),
            "session_discovered": bool(session_id),
        }
    except (OSError, URLError, ValueError) as exc:
        return {"status": "blocked", "healthy": False, **base, "error": str(exc)}


def _ollama_health() -> dict[str, Any]:
    from .ollama import OllamaClient, OllamaConfig, OllamaError

    config = OllamaConfig.from_env()
    try:
        models = OllamaClient(config).list_models()
        names = [str(item.get("name") or item.get("model")) for item in models]
        expected = config.default_model
        model_available = not expected or expected in names
        return {
            "status": "ready" if model_available else "degraded",
            "endpoint": config.host,
            "healthy": True,
            "model": expected,
            "model_available": model_available,
            "models": names[:32],
            "transport": "ollama-http",
        }
    except (OSError, OllamaError, ValueError) as exc:
        return {
            "status": "blocked",
            "endpoint": config.host,
            "healthy": False,
            "model": config.default_model,
            "transport": "ollama-http",
            "error": str(exc),
        }


def _executable_health(*, env_names: tuple[str, ...], defaults: tuple[str, ...], transport: str) -> dict[str, Any]:
    executable = _executable_from_env(*env_names, defaults=defaults)
    if executable:
        return {
            "status": "unknown",
            "healthy": None,
            "executable": executable,
            "transport": transport,
            "probe": "executable-only",
        }
    return {
        "status": "blocked",
        "healthy": False,
        "transport": transport,
        "error": f"executable not found; set {env_names[0]}",
    }


def _local_qwen_prerequisite_health() -> dict[str, Any]:
    codex = _executable_from_env("AR_CODEX_BIN", defaults=("codex", "codex.exe"))
    ollama = _executable_from_env("AR_OLLAMA_BIN", defaults=("ollama", "ollama.exe"))
    missing = []
    if not codex:
        missing.append("codex")
    if not ollama:
        missing.append("ollama")
    return {
        "status": "unknown" if not missing else "blocked",
        "healthy": None if not missing else False,
        "executables": {"codex": codex, "ollama": ollama},
        "transport": "codex-cli + ollama",
        "probe": "executable-only",
        **({} if not missing else {"error": f"missing executable(s): {', '.join(missing)}"}),
    }


def lane_health_manifest(*, probe: bool = False) -> list[dict[str, Any]]:
    """Return truthful lane readiness without changing the routing registry.

    ``lane_manifest`` is intentionally descriptive and side-effect free.  This
    function is the operational view: with ``probe=False`` it checks cheap
    local prerequisites, and with ``probe=True`` it also calls the configured
    Ollama and Claude bridge health endpoints.
    """

    load_dotenv()
    checks: dict[str, Callable[[], dict[str, Any]]] = {
        "local-qwen": lambda: _ollama_health() if probe else _local_qwen_prerequisite_health(),
        "claude-task": lambda: _claude_health(probe_endpoint=probe),
        "claude-mcp": lambda: _claude_mcp_health(probe_endpoint=probe),
        "sol-reviewer": lambda: _executable_health(
            env_names=("AR_CODEX_BIN",), defaults=("codex", "codex.exe"), transport="codex-cli"
        ),
        "agy-antigravity": lambda: _executable_health(
            env_names=("AR_AGY_BIN",), defaults=("agy", "agy.exe"), transport="agy-cli"
        ),
    }
    result: list[dict[str, Any]] = []
    for lane in (_lane_with_env_overrides(item) for item in LANES):
        item = lane.to_dict()
        item["health"] = checks[lane.name]()
        item["status"] = item["health"]["status"]
        result.append(item)
    return result
