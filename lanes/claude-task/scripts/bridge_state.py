"""Small durable state layer for Claude Orchestrator profiles and jobs.

This stores bounded task receipts, explicit memories, and reviewed skill
snippets outside the project worktree. It never stores Claude transcripts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

MAX_MEMORY_CHARS = 4_000
MAX_SKILL_CHARS = 20_000
MAX_MEMORY_RESULTS = 8
SAFE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


class StateError(ValueError):
    pass


def utc_epoch() -> float:
    return time.time()


def safe_name(value: str, label: str = "name") -> str:
    if not isinstance(value, str) or not SAFE_NAME.fullmatch(value):
        raise StateError(f"{label} must be a safe identifier")
    return value


def atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class ProfileStore:
    def __init__(self, root: Path, profile: str = "default") -> None:
        self.root = root.resolve()
        self.profile = safe_name(profile, "profile")
        self.profile_root = self.root / "profiles" / self.profile
        self.memory_path = self.profile_root / "memory.jsonl"
        self.skills_root = self.profile_root / "skills"

    def add_memory(self, *, text: str, kind: str = "lesson", tags: list[str] | None = None, source_task_id: str | None = None) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_MEMORY_CHARS:
            raise StateError(f"memory text must be non-empty and at most {MAX_MEMORY_CHARS} characters")
        record = {
            "memory_id": hashlib.sha256(f"{utc_epoch()}\n{text}".encode("utf-8")).hexdigest()[:16],
            "created_at": utc_epoch(),
            "kind": kind[:80],
            "tags": [str(tag)[:40] for tag in (tags or [])[:16]],
            "source_task_id": source_task_id,
            "text": text,
        }
        self.profile_root.mkdir(parents=True, exist_ok=True)
        with self.memory_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        return record

    def search_memory(self, query: str | None, limit: int = MAX_MEMORY_RESULTS) -> list[dict[str, Any]]:
        if not self.memory_path.is_file():
            return []
        words = {word.lower() for word in re.findall(r"[\w-]{2,}", query or "")}
        records: list[tuple[int, float, dict[str, Any]]] = []
        for line in self.memory_path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = str(record.get("text", ""))
            score = len(words & {word.lower() for word in re.findall(r"[\w-]{2,}", text)}) if words else 0
            records.append((score, float(record.get("created_at", 0)), record))
        records.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [record for _, _, record in records[: max(1, min(limit, MAX_MEMORY_RESULTS))]]

    def read_skill(self, skill_ref: str) -> str | None:
        skill = safe_name(skill_ref, "skill_ref")
        path = self.skills_root / f"{skill}.md"
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")[:MAX_SKILL_CHARS]

    def write_skill(self, skill_ref: str, content: str) -> dict[str, Any]:
        skill = safe_name(skill_ref, "skill_ref")
        if not isinstance(content, str) or not content.strip() or len(content) > MAX_SKILL_CHARS:
            raise StateError(f"skill content must be non-empty and at most {MAX_SKILL_CHARS} characters")
        path = self.skills_root / f"{skill}.md"
        atomic_write(path, content)
        return {"skill_ref": skill, "path": str(path), "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(), "updated_at": utc_epoch()}

    def list_skills(self) -> list[str]:
        if not self.skills_root.is_dir():
            return []
        return sorted(path.stem for path in self.skills_root.glob("*.md") if path.is_file())
