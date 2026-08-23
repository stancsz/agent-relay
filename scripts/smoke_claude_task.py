"""Run one real Claude-task lane smoke in a disposable Git repository."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.claude_task import ClaudeTaskConfig, run_claude_task
from agent_relay.task import DelegationTask


def _git(root: Path, *args: str) -> None:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip()[:1_000])


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="agent-relay-claude-task-smoke-") as raw:
        root = Path(raw)
        (root / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
        _git(root, "init", "--quiet")
        _git(root, "config", "user.name", "Agent Relay Smoke")
        _git(root, "config", "user.email", "agent-relay-smoke@example.invalid")
        _git(root, "add", "value.py")
        _git(root, "commit", "--quiet", "-m", "smoke baseline")
        task = DelegationTask(
            task_id="real-claude-task-smoke",
            objective="Change VALUE from 1 to 2.",
            allowed_files=("value.py",),
            context=("value.py",),
            requirements=("VALUE must equal 2 after the change.",),
            constraints=("Do not touch files outside allowed_files.",),
            verification=("python -c \"from value import VALUE; assert VALUE == 2\"",),
            success_criteria=("The declared verification command exits 0.",),
            task_kind="mechanical",
        )
        result = run_claude_task(
            task,
            root,
            config=ClaudeTaskConfig.from_env(
                timeout_seconds=120,
            ),
        )
        handoff = result.to_handoff()
        report = {
            "status": "PASS" if result.success else "FAIL",
            "result_status": result.status.value,
            "summary": result.summary,
            "files_changed": list(result.files_changed),
            "verification": [item.to_dict() for item in result.verification],
            "main_worktree_unchanged": result.metadata.get("main_worktree_unchanged"),
            "lane": result.metadata.get("lane"),
            "transport": result.metadata.get("transport"),
            "server_receipt": result.metadata.get("server_receipt"),
            "handoff": handoff,
        }
        # Keep the report portable on Windows consoles that still default to
        # CP1252; JSON escapes preserve the exact receipt without print-time
        # encoding failures.
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
