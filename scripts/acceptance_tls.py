"""Exercise the coordinator over HTTPS with a temporary self-signed CA cert."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.control import ControlPlaneError, request_json  # noqa: E402

TOKEN = "tls-acceptance-token"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _openssl() -> str | None:
    candidates = [shutil.which("openssl")]
    if sys.platform == "win32":
        candidates.append(r"C:\Program Files\Git\usr\bin\openssl.exe")
    return next((item for item in candidates if item and Path(item).is_file()), None)


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if sys.platform == "win32":
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


def run() -> dict[str, object]:
    openssl = _openssl()
    if openssl is None:
        return {"status": "BLOCKED", "reason": "openssl executable is unavailable"}
    with tempfile.TemporaryDirectory(prefix="agent-relay-tls-") as raw:
        root = Path(raw)
        cert = root / "server.pem"
        key = root / "server.key"
        generated = subprocess.run(
            [
                openssl,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-subj",
                "/CN=127.0.0.1",
                "-addext",
                "subjectAltName=IP:127.0.0.1",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if generated.returncode != 0:
            raise RuntimeError((generated.stderr or generated.stdout)[-2_000:])

        port = _free_port()
        database = root / "relay.sqlite3"
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
                TOKEN,
                "--tls-cert",
                str(cert),
                "--tls-key",
                str(key),
            ],
            cwd=str(ROOT),
            env={
                **dict(os.environ),
                "PYTHONPATH": str(ROOT / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""),
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            base = f"https://127.0.0.1:{port}"
            os.environ["AR_RELAY_CA_CERT"] = str(cert)
            deadline = time.monotonic() + 10
            health: dict[str, object] | None = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("coordinator exited during TLS startup")
                try:
                    health = request_json(base, "GET", "/health")
                    break
                except (ControlPlaneError, OSError, URLError):
                    time.sleep(0.05)
            if health is None:
                raise RuntimeError("HTTPS coordinator did not become healthy")
            listing = request_json(base, "GET", "/tasks", auth_token=TOKEN)
            unauthenticated = False
            try:
                request_json(base, "GET", "/tasks")
            except ControlPlaneError as exc:
                unauthenticated = "HTTP 401" in str(exc)
            return {
                "status": "PASS",
                "health_tls": health.get("tls") is True,
                "authenticated_request": isinstance(listing.get("tasks"), list),
                "unauthenticated_rejected": unauthenticated,
            }
        finally:
            _stop(process)


def main() -> int:
    try:
        report = run()
    except Exception as exc:
        report = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
