from __future__ import annotations

from argparse import Namespace
import sys
from pathlib import Path

import pytest

SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "lanes" / "claude-task" / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from a2a_protocol import ProtocolError, build_task, digest_without_context_digest  # noqa: E402
from claude_a2a_server import A2AState  # noqa: E402


def _state(tmp_path: Path) -> A2AState:
    state = A2AState(
        Namespace(
            workspace_root=str(tmp_path),
            auth_token="secret",
            worker_agent_type=None,
            verifier_agent_type=None,
            agents_json=None,
            cli_fallback=True,
            no_cli_fallback=False,
            timeout_seconds=30,
            state_dir=str(tmp_path / "state"),
        )
    )
    state._start_job = lambda _job_id: None
    return state


def test_async_claude_jobs_reject_task_id_reuse_with_changed_digest(tmp_path: Path) -> None:
    state = _state(tmp_path)
    first = build_task(
        task_id="async-replay-task",
        target_role="worker",
        operation="work",
        target_paths=["value.py"],
        objective="Make the bounded change.",
        acceptance_criteria=["The task completes."],
        constraints=["Do not edit unrelated files."],
        inputs=[],
    )
    assert state.enqueue_job(first)["job_id"].startswith("async-replay-task-")
    assert state.enqueue_job(first)["task_id"] == "async-replay-task"

    changed = dict(first)
    changed["objective"] = "Make a different bounded change."
    changed["context_digest"] = digest_without_context_digest(changed)
    with pytest.raises(ProtocolError, match="different context digest"):
        state.enqueue_job(changed)
