"""Fault-inject a real Claude worker process and verify lease recovery.

This is deliberately separate from the physical LAN gate: the coordinator is
loopback, but the worker and its Claude bridge run in separate processes. The
worker is stopped after the coordinator observes ``running``; a second worker
must reclaim the expired lease and produce a verified receipt.
"""

from __future__ import annotations

import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.control import ControlPlaneError, create_server, request_json
from agent_relay.protocol import JobState
from agent_relay.task import DelegationTask
from agent_relay.worker_plane import WorkerConfig, run_worker_once


ADMIN_TOKEN = "claude-interruption-smoke-admin"
TASK_ID = "real-claude-interruption-smoke"


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


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
            )
        else:
            process.kill()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=8)


def _worker_process(base: str, repo: Path, worker_id: str, lease_seconds: float) -> subprocess.Popen[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + environment.get("PYTHONPATH", "")
    return subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-c",
            (
                "from pathlib import Path; "
                "from agent_relay.worker_plane import WorkerConfig, run_worker_once; "
                "import json; "
                f"c=WorkerConfig(coordinator_url={base!r}, auth_token={ADMIN_TOKEN!r}, "
                f"worker_id={worker_id!r}, repo=Path({str(repo)!r}), backend='claude-task', "
                f"lease_seconds={lease_seconds!r}, poll_seconds=0.2); "
                "print(json.dumps(run_worker_once(c), ensure_ascii=True), flush=True)"
            ),
        ],
        cwd=str(ROOT),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="agent-relay-claude-interruption-") as raw:
        root = Path(raw)
        repo = root / "repo"
        repo.mkdir()
        (repo / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
        _git(repo, "init", "--quiet")
        _git(repo, "config", "user.name", "Agent Relay Interruption Smoke")
        _git(repo, "config", "user.email", "agent-relay-interruption@example.invalid")
        _git(repo, "add", "value.py")
        _git(repo, "commit", "--quiet", "-m", "interruption smoke baseline")

        task = DelegationTask(
            task_id=TASK_ID,
            objective="Change VALUE from 1 to 2 in value.py. Inspect before editing; do not commit.",
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
            database=root / "relay.sqlite3",
            auth_token=ADMIN_TOKEN,
        )
        server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        server_thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        first: subprocess.Popen[str] | None = None
        second: subprocess.Popen[str] | None = None
        third: subprocess.Popen[str] | None = None
        try:
            request_json(
                base,
                "POST",
                "/tasks",
                auth_token=ADMIN_TOKEN,
                payload={"task": task.to_dict(), "idempotency_key": TASK_ID},
            )
            first = _worker_process(base, repo, "interrupted-worker", 2.0)
            deadline = time.monotonic() + 40
            running_observed = False
            while time.monotonic() < deadline:
                current = request_json(base, "GET", f"/tasks/{TASK_ID}", auth_token=ADMIN_TOKEN)
                if current.get("state") == JobState.RUNNING.value:
                    running_observed = True
                    break
                if first.poll() is not None:
                    break
                time.sleep(0.1)
            if not running_observed:
                raise RuntimeError("first worker did not reach running before interruption")

            _stop(first)
            first_stdout, first_stderr = first.communicate(timeout=5)
            time.sleep(2.5)
            before_recovery = request_json(base, "GET", f"/tasks/{TASK_ID}", auth_token=ADMIN_TOKEN)

            second = _worker_process(base, repo, "recovery-worker", 60.0)
            try:
                second_stdout, second_stderr = second.communicate(timeout=180)
            except subprocess.TimeoutExpired:
                _stop(second)
                second_stdout, second_stderr = second.communicate(timeout=8)
                raise RuntimeError("recovery worker timed out")
            third = _worker_process(base, repo, "recovery-worker-2", 60.0)
            try:
                third_stdout, third_stderr = third.communicate(timeout=180)
            except subprocess.TimeoutExpired:
                _stop(third)
                third_stdout, third_stderr = third.communicate(timeout=8)
                raise RuntimeError("bounded retry worker timed out")
            final = request_json(base, "GET", f"/tasks/{TASK_ID}", auth_token=ADMIN_TOKEN)
            receipt = final.get("receipt") if isinstance(final.get("receipt"), dict) else {}
            evidence = receipt.get("evidence") if isinstance(receipt, dict) else {}
            events = final.get("events", [])
            lease_expired_observed = any(
                isinstance(event, dict) and "lease expired" in str(event.get("reason", ""))
                for event in events
            )
            reassigned_observed = any(
                isinstance(event, dict)
                and event.get("reason") == "lease assigned"
                and isinstance(event.get("data"), dict)
                and event["data"].get("worker_id") == "recovery-worker"
                for event in events
            )
            passed = (
                running_observed
                and before_recovery.get("state") == JobState.RUNNING.value
                and lease_expired_observed
                and reassigned_observed
                and final.get("state") == JobState.SUCCEEDED.value
                and isinstance(evidence, dict)
                and evidence.get("main_worktree_unchanged") is True
            )
            return {
                "status": "PASS" if passed else "FAIL",
                "running_observed": running_observed,
                "state_before_recovery": before_recovery.get("state"),
                "lease_expired_observed": lease_expired_observed,
                "reassigned_observed": reassigned_observed,
                "final_state": final.get("state"),
                "event_count": len(events),
                "event_tail": events[-6:],
                "receipt_evidence": evidence,
                "first_worker_stderr_tail": first_stderr[-1_000:],
                "second_worker_stdout_tail": second_stdout[-1_000:],
                "second_worker_stderr_tail": second_stderr[-1_000:],
                "third_worker_stdout_tail": third_stdout[-1_000:],
                "third_worker_stderr_tail": third_stderr[-1_000:],
            }
        finally:
            if first is not None:
                _stop(first)
            if second is not None:
                _stop(second)
            if third is not None:
                _stop(third)
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)


def main() -> int:
    try:
        report = run()
    except (ControlPlaneError, OSError, RuntimeError, ValueError) as exc:
        report = {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
