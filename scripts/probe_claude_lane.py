"""Probe the real Claude A2A bridge without editing the caller repository."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "lanes" / "claude-task" / "scripts" / "claude_a2a_server.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get_json(url: str, timeout: float) -> tuple[int, dict[str, object]]:
    try:
        with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
            return response.status, value if isinstance(value, dict) else {"value": value}
    except HTTPError as exc:
        raw = exc.read(32_000).decode("utf-8", errors="replace")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = {"error": raw[:2_000]}
        return exc.code, value if isinstance(value, dict) else {"value": value}
    except (OSError, URLError, json.JSONDecodeError) as exc:
        return 0, {"error": f"{type(exc).__name__}: {exc}"}


def _stop(process: subprocess.Popen[str]) -> tuple[str, str]:
    if process.poll() is None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
            )
        else:
            process.terminate()
    try:
        stdout, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
    return stdout[-2_000:], stderr[-2_000:]


def run(timeout: float) -> dict[str, object]:
    port = _free_port()
    with tempfile.TemporaryDirectory(prefix="agent-relay-claude-probe-") as raw:
        workspace = Path(raw)
        state_dir = workspace / "state"
        command = [
            sys.executable,
            "-B",
            str(SERVER),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workspace-root",
            str(workspace),
            "--cli-fallback",
            "--state-dir",
            str(state_dir),
            "--timeout-seconds",
            str(max(1, int(timeout))),
        ]
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(os.environ),
        )
        try:
            base = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + min(15.0, timeout)
            health: dict[str, object] = {}
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                status, health = _get_json(f"{base}/health", 1.0)
                if status == 200 and health.get("healthy") is True:
                    break
                time.sleep(0.1)
            health_status, health = _get_json(f"{base}/health", 2.0)
            capability_status, capabilities = _get_json(f"{base}/capabilities", timeout)
            result = {
                "status": "PASS" if health_status == 200 and capabilities.get("healthy") is True else "BLOCKED",
                "health_status": health_status,
                "health": health,
                "capability_status": capability_status,
                "capabilities": capabilities,
                "server_pid": process.pid,
            }
            return result
        finally:
            stdout, stderr = _stop(process)
            # Do not include arbitrary Claude output in the result; retain a
            # bounded tail only for diagnosing a failed capability probe.
            if stdout.strip() or stderr.strip():
                result = locals().get("result")
                if isinstance(result, dict):
                    result["server_stdout_tail"] = stdout
                    result["server_stderr_tail"] = stderr


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=40.0)
    args = parser.parse_args()
    try:
        report = run(args.timeout)
    except Exception as exc:
        report = {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
