"""Exercise real Claude cancellation through the Agent Relay worker plane."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.control import ControlPlaneError, create_server, request_json
from agent_relay.protocol import JobState
from agent_relay.task import DelegationTask
from agent_relay.worker_plane import WorkerConfig, run_worker_once


ADMIN_TOKEN = "claude-cancel-smoke-admin"
TASK_ID = "real-claude-cancellation-smoke"


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


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="agent-relay-claude-cancel-") as raw:
        repo = Path(raw) / "repo"
        repo.mkdir()
        (repo / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
        _git(repo, "init", "--quiet")
        _git(repo, "config", "user.name", "Agent Relay Cancellation Smoke")
        _git(repo, "config", "user.email", "agent-relay-cancel@example.invalid")
        _git(repo, "add", "value.py")
        _git(repo, "commit", "--quiet", "-m", "cancellation smoke baseline")

        task = DelegationTask(
            task_id=TASK_ID,
            objective="Change VALUE from 1 to 2 in value.py. Inspect the file before editing and do not commit.",
            allowed_files=("value.py",),
            context=("value.py",),
            requirements=("Only value.py may change.",),
            constraints=("Do not commit, push, merge, or edit files outside allowed_files.",),
            verification=("python -c \"from value import VALUE; assert VALUE == 2\"",),
            success_criteria=("VALUE equals 2 after the change.",),
            task_kind="mechanical",
        )
        server = create_server(
            host="127.0.0.1",
            port=0,
            database=Path(raw) / "relay.sqlite3",
            auth_token=ADMIN_TOKEN,
        )
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        worker = WorkerConfig(
            coordinator_url=base,
            auth_token=ADMIN_TOKEN,
            worker_id="claude-cancel-worker",
            repo=repo,
            backend="claude-task",
            lease_seconds=60,
            poll_seconds=0.2,
        )
        try:
            request_json(
                base,
                "POST",
                "/tasks",
                auth_token=ADMIN_TOKEN,
                payload={"task": task.to_dict(), "idempotency_key": TASK_ID},
            )
            outcomes: list[dict[str, object]] = []
            worker_thread = threading.Thread(target=lambda: outcomes.extend(run_worker_once(worker)), daemon=True)
            worker_thread.start()
            deadline = time.monotonic() + 30
            running = False
            while time.monotonic() < deadline:
                current = request_json(base, "GET", f"/tasks/{TASK_ID}", auth_token=ADMIN_TOKEN)
                if current.get("state") == JobState.RUNNING.value:
                    running = True
                    break
                if current.get("state") in {item.value for item in (JobState.SUCCEEDED, JobState.FAILED, JobState.BLOCKED, JobState.CANCELLED)}:
                    break
                time.sleep(0.1)
            if not running:
                raise RuntimeError("Claude worker did not reach running before the cancellation window")
            requested = request_json(base, "POST", f"/tasks/{TASK_ID}/cancel", auth_token=ADMIN_TOKEN, payload={"actor": "client"})
            worker_thread.join(timeout=150)
            final = request_json(base, "GET", f"/tasks/{TASK_ID}", auth_token=ADMIN_TOKEN)
            receipt = final.get("receipt") or {}
            evidence = receipt.get("evidence") if isinstance(receipt, dict) else {}
            passed = (
                not worker_thread.is_alive()
                and requested.get("state") == JobState.CANCEL_REQUESTED.value
                and final.get("state") == JobState.CANCELLED.value
                and isinstance(evidence, dict)
                and evidence.get("cancel_requested") is True
                and evidence.get("execution_stopped") is True
            )
            outcome_summary = []
            for outcome in outcomes:
                envelope = outcome.get("result") if isinstance(outcome, dict) else None
                receipt_raw = envelope.get("receipt") if isinstance(envelope, dict) else None
                receipt_evidence = receipt_raw.get("evidence") if isinstance(receipt_raw, dict) else {}
                outcome_summary.append(
                    {
                        "task_id": outcome.get("task_id"),
                        "status": outcome.get("status"),
                        "event_count": len(envelope.get("events", [])) if isinstance(envelope, dict) else None,
                        "execution_stopped": receipt_evidence.get("execution_stopped") if isinstance(receipt_evidence, dict) else None,
                    }
                )
            return {
                "status": "PASS" if passed else "FAIL",
                "running_observed": running,
                "cancel_requested_state": requested.get("state"),
                "final_state": final.get("state"),
                "execution_stopped": evidence.get("execution_stopped") if isinstance(evidence, dict) else None,
                "outcomes": outcome_summary,
            }
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def main() -> int:
    try:
        report = run()
    except (ControlPlaneError, OSError, RuntimeError, ValueError) as exc:
        report = {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
