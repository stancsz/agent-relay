"""Canonical subagent lane registry.

The registry is deliberately descriptive.  It keeps routing names and their
authority boundaries in one place without pretending that every lane shares a
transport or a model runtime.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class SubagentLane:
    name: str
    role: str
    execution: str
    model: str
    reasoning: str | None
    mutates_worktree: bool
    verification: str
    status: str = "implemented"

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
        role="primary implementation/team worker",
        execution="Authenticated Claude Code task bridge with optional Agent Teams",
        model="host policy",
        reasoning=None,
        mutates_worktree=True,
        verification="bounded task receipt, Git/workspace gates, parent tests",
    ),
    SubagentLane(
        name="codex-review",
        role="independent verifier",
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

_ALIASES = {
    "ollama": "local-qwen",
    "codex-ollama": "local-qwen",
    "qwen": "local-qwen",
    "claude": "claude-task",
    "claude-team": "claude-task",
    "review": "codex-review",
    "codex-verifier": "codex-review",
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


def lane_manifest() -> list[dict[str, Any]]:
    return [lane.to_dict() for lane in LANES]
