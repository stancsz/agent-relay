"""CLI client for the claude-a2a HTTP relay."""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from typing import Any

from a2a_protocol import ProtocolError, validate_result, validate_task


for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError):
        pass


def request_json(url: str, method: str, token: str | None, body: bytes | None = None, timeout: float | None = None) -> tuple[int, Any]:
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        context = None
        if url.lower().startswith("https://"):
            context = ssl.create_default_context(cafile=os.environ.get("CLAUDE_A2A_CA_CERT"))
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": raw}
        return exc.code, payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Submit a bounded claude-a2a task.")
    parser.add_argument("--server-url", default=os.environ.get("CLAUDE_A2A_SERVER_URL", "http://127.0.0.1:8787"))
    parser.add_argument("--task-file")
    parser.add_argument("--auth-token", default=os.environ.get("CLAUDE_A2A_AUTH_TOKEN"))
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--probe-capabilities", action="store_true", help="also start a fresh MCP session and report Claude native capabilities")
    parser.add_argument("--async", dest="asynchronous", action="store_true", help="queue the task in the durable daemon and return a job receipt")
    parser.add_argument("--watch", action="store_true", help="wait for an asynchronous job to finish")
    parser.add_argument("--job-id")
    parser.add_argument("--cancel-job", action="store_true")
    parser.add_argument("--resume-job", action="store_true")
    parser.add_argument("--list-jobs", action="store_true")
    parser.add_argument("--list-goals", action="store_true")
    parser.add_argument("--memory-query")
    parser.add_argument("--profile", default="default")
    args = parser.parse_args()
    base = args.server_url.rstrip("/")
    if args.health:
        status, payload = request_json(f"{base}/health", "GET", args.auth_token, timeout=args.timeout_seconds)
        if args.probe_capabilities and status == 200 and payload.get("healthy"):
            capability_status, capabilities = request_json(f"{base}/capabilities", "GET", args.auth_token, timeout=args.timeout_seconds)
            payload["capabilities"] = capabilities
            status = capability_status
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if status == 200 and payload.get("healthy") and (not args.probe_capabilities or payload.get("capabilities", {}).get("healthy")) else 1
    if args.list_jobs:
        status, payload = request_json(f"{base}/a2a/jobs", "GET", args.auth_token, timeout=args.timeout_seconds)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if status == 200 else 1
    if args.list_goals:
        status, payload = request_json(f"{base}/a2a/goals", "GET", args.auth_token, timeout=args.timeout_seconds)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if status == 200 else 1
    if args.memory_query is not None:
        query = urllib.parse.urlencode({"profile": args.profile, "q": args.memory_query})
        status, payload = request_json(f"{base}/a2a/memory?{query}", "GET", args.auth_token, timeout=args.timeout_seconds)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if status == 200 else 1
    if args.job_id and (args.cancel_job or args.resume_job):
        action = "cancel" if args.cancel_job else "resume"
        status, payload = request_json(f"{base}/a2a/jobs/{urllib.parse.quote(args.job_id, safe='')}/{action}", "POST", args.auth_token, b"{}", args.timeout_seconds)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if status == 200 else 1
    if args.job_id:
        while True:
            status, payload = request_json(f"{base}/a2a/jobs/{urllib.parse.quote(args.job_id, safe='')}", "GET", args.auth_token, timeout=args.timeout_seconds)
            if not args.watch or payload.get("status") in {"done", "failed", "blocked", "cancelled", "interrupted"} or status != 200:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return 0 if status == 200 and payload.get("status") == "done" else 1
            time.sleep(1)
    raw = open(args.task_file, "r", encoding="utf-8").read() if args.task_file else sys.stdin.read()
    try:
        task = validate_task(json.loads(raw))
    except (json.JSONDecodeError, ProtocolError) as exc:
        print(json.dumps({"protocol": "claude-a2a/0.1", "error": f"invalid task: {exc}"}, ensure_ascii=False))
        return 2
    endpoint = "/a2a/jobs" if args.asynchronous else "/a2a/tasks"
    status, payload = request_json(f"{base}{endpoint}", "POST", args.auth_token, json.dumps(task, ensure_ascii=False).encode("utf-8"), args.timeout_seconds)
    if args.asynchronous:
        if args.watch and status == 202:
            job_id = payload.get("job_id")
            while job_id:
                time.sleep(1)
                poll_status, poll_payload = request_json(f"{base}/a2a/jobs/{urllib.parse.quote(job_id, safe='')}", "GET", args.auth_token, timeout=args.timeout_seconds)
                payload = poll_payload
                status = poll_status
                if payload.get("status") in {"done", "failed", "blocked", "cancelled", "interrupted"}:
                    break
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if status in {200, 202} and payload.get("status") == "done" else 1
    try:
        if status == 200:
            validate_result(payload)
    except ProtocolError as exc:
        payload = {"protocol": "claude-a2a/0.1", "error": f"invalid server result: {exc}", "raw": payload}
        status = 502
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if status == 200 and payload.get("status") == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
