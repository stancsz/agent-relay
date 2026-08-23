"""Run the authenticated Agent Relay control-plane acceptance scenario.

This is a deterministic loopback harness for the two-PC protocol roles. It
uses real HTTP requests, a separate coordinator process, separate enrolled
worker credentials, a coordinator restart, and an injected lease-expiry
boundary. Run it before the physical LAN scenario; the output is intentionally
a compact proof packet rather than an assertion-only test.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sys
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.control import ControlPlaneError, request_json, stream_events  # noqa: E402
from agent_relay.protocol import ArtifactRef, JobReceipt, JobState, utc_now  # noqa: E402


ADMIN_TOKEN = "acceptance-admin-token"
WORKER_A_TOKEN = "acceptance-worker-a-token"
WORKER_B_TOKEN = "acceptance-worker-b-token"


def _task_payload() -> dict[str, object]:
    return {
        "task_id": "acceptance-task",
        "objective": "Change one bounded file and return a verified patch.",
        "allowed_files": ["value.py"],
        "verification": ["python -c \"assert True\""],
        "task_kind": "mechanical",
    }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _server(database: Path):
    port = _free_port()
    environment = dict(os.environ)
    source_path = str(ROOT / "src")
    environment["PYTHONPATH"] = source_path + os.pathsep + environment.get("PYTHONPATH", "")
    process = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-m",
            "agent_relay.cli",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--db",
            str(database),
            "--token",
            ADMIN_TOKEN,
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
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read()[-2_000:] if process.stderr else ""
            raise RuntimeError(f"coordinator exited during startup: {stderr}")
        try:
            health = request_json(base, "GET", "/health")
            if health.get("healthy") is True:
                return process, base
            last_error = str(health)
        except ControlPlaneError as exc:
            last_error = str(exc)
        time.sleep(0.05)
    _stop(process)
    raise RuntimeError(f"coordinator did not become healthy: {last_error}")


def _stop(process) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            else:
                process.kill()
            process.wait(timeout=3)
    try:
        process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=3)


def _agent_headers(agent_id: str, token: str) -> dict[str, str]:
    return {"agent_id": agent_id, "agent_token": token}


def _register(base: str, agent_id: str, token: str) -> None:
    request_json(
        base,
        "POST",
        "/agents/register",
        auth_token=ADMIN_TOKEN,
        **_agent_headers(agent_id, token),
        payload={
            "agent_id": agent_id,
            "name": agent_id,
            "readiness": "unknown",
            "capabilities": ["bounded-edit"],
            "task_kinds": ["mechanical"],
            "transports": ["agent-relay-http"],
        },
    )


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="agent-relay-acceptance-") as raw_dir:
        database = Path(raw_dir) / "relay.sqlite3"
        server, base = _server(database)
        try:
            _register(base, "worker-a", WORKER_A_TOKEN)
            _register(base, "worker-b", WORKER_B_TOKEN)
            submitted = request_json(
                base,
                "POST",
                "/tasks",
                auth_token=ADMIN_TOKEN,
                payload={"task": _task_payload(), "idempotency_key": "acceptance-idempotency"},
            )
            duplicate = request_json(
                base,
                "POST",
                "/tasks",
                auth_token=ADMIN_TOKEN,
                payload={"task": _task_payload(), "idempotency_key": "acceptance-idempotency"},
            )
            assert submitted["created"] is True and duplicate["created"] is False

            worker_a = _agent_headers("worker-a", WORKER_A_TOKEN)
            worker_b = _agent_headers("worker-b", WORKER_B_TOKEN)
            first_lease = request_json(
                base,
                "POST",
                "/tasks/acceptance-task/leases",
                payload={"worker_id": "worker-a", "ttl_seconds": 1},
                **worker_a,
            )
            lease_a = first_lease["lease"]["lease_id"]
            request_json(
                base,
                "POST",
                "/tasks/acceptance-task/transition",
                payload={"state": "running", "actor": "worker-a", "lease_id": lease_a, "reason": "worker A started"},
                **worker_a,
            )
            time.sleep(1.15)
            second_lease = request_json(
                base,
                "POST",
                "/tasks/acceptance-task/leases",
                payload={"worker_id": "worker-b", "ttl_seconds": 30},
                **worker_b,
            )
            lease_b = second_lease["lease"]["lease_id"]
            stale_rejected = False
            try:
                request_json(
                    base,
                    "POST",
                    "/tasks/acceptance-task/transition",
                    payload={"state": "failed", "actor": "worker-a", "lease_id": lease_a, "reason": "stale worker"},
                    **worker_a,
                )
            except ControlPlaneError as exc:
                stale_rejected = "409" in str(exc)
            assert stale_rejected

            request_json(
                base,
                "POST",
                "/tasks/acceptance-task/transition",
                payload={"state": "running", "actor": "worker-b", "lease_id": lease_b, "reason": "worker B resumed"},
                **worker_b,
            )
            artifact_raw = request_json(
                base,
                "POST",
                "/tasks/acceptance-task/artifacts",
                payload={
                    "name": "acceptance.patch",
                    "content": "diff --git a/value.py b/value.py\n",
                    "kind": "patch",
                    "media_type": "text/x-diff",
                    "provenance": "worker-b",
                },
                **worker_b,
            )["artifact"]
            artifact = ArtifactRef.from_dict(artifact_raw)
            receipt = JobReceipt(
                receipt_id="acceptance-receipt",
                task_id="acceptance-task",
                final_state=JobState.SUCCEEDED,
                actor="worker-b",
                completed_at=utc_now(),
                evidence={"execution": "fault-injection recovery", "verification": "passed"},
                artifacts=(artifact,),
                workspace={"repo": "acceptance-fixture", "sandbox_mode": "adapter-owned"},
                summary="Recovered by worker B after worker A lease expiry.",
            )
            terminal = request_json(
                base,
                "POST",
                "/tasks/acceptance-task/transition",
                payload={
                    "state": "succeeded",
                    "actor": "worker-b",
                    "lease_id": lease_b,
                    "reason": "verified artifact returned",
                    "evidence": dict(receipt.evidence),
                    "receipt": receipt.to_dict(),
                },
                **worker_b,
            )
            assert terminal["state"] == "succeeded"
        finally:
            _stop(server)

        restarted, restarted_base = _server(database)
        try:
            inspected = request_json(restarted_base, "GET", "/tasks/acceptance-task", auth_token=ADMIN_TOKEN)
            events = list(stream_events(restarted_base, "/tasks/acceptance-task/events/stream?after=0&timeout=2", auth_token=ADMIN_TOKEN))
            scoped_after_restart = request_json(restarted_base, "GET", "/tasks", **_agent_headers("worker-b", WORKER_B_TOKEN))
            revoked = request_json(restarted_base, "POST", "/agents/worker-b/revoke", payload={}, auth_token=ADMIN_TOKEN)
            revoke_rejected = False
            try:
                request_json(restarted_base, "GET", "/tasks", **_agent_headers("worker-b", WORKER_B_TOKEN))
            except ControlPlaneError as exc:
                revoke_rejected = "401" in str(exc)
            assert inspected["state"] == "succeeded"
            assert events[-1]["data"]["state"] == "succeeded"
            assert scoped_after_restart["tasks"][0]["task"]["task_id"] == "acceptance-task"
            assert revoked["agent"]["metadata"]["revoked"] is True
            assert revoke_rejected
            return {
                "status": "PASS",
                "checks": {
                    "idempotent_submit": duplicate["created"] is False,
                    "expired_lease_reassigned": second_lease["lease"]["worker_id"] == "worker-b",
                    "stale_worker_rejected": stale_rejected,
                    "artifact_receipt_terminal": terminal["state"] == "succeeded",
                    "restart_reconnect": inspected["state"] == "succeeded" and events[-1]["data"]["state"] == "succeeded",
                    "scoped_credential_survives_restart": scoped_after_restart["tasks"][0]["task"]["task_id"] == "acceptance-task",
                    "revocation_enforced": revoke_rejected,
                },
                "task_id": "acceptance-task",
                "event_count": len(events),
                "artifact_sha256": artifact.sha256,
            }
        finally:
            _stop(restarted)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        report = run()
    except Exception as exc:
        report = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
