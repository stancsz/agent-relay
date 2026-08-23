from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.parse import quote

from .codex_worker import CodexCliConfig, CodexCliError, CodexCliWorker
from .codex_review import CodexReviewConfig, run_codex_review
from .agy_antigravity import AgyConfig, run_agy
from .claude_task import run_claude_task
from .claude_mcp import run_claude_mcp_task
from .control import (
    ControlPlaneError,
    default_auth_token,
    default_agent_token,
    default_database,
    request_json,
    serve_forever,
    stream_events,
)
from .delegate import delegate_local
from .lanes import canonical_lane_name, lane_health_manifest, lane_manifest
from .mcp import serve_mcp_forever
from .ollama import OllamaClient, OllamaConfig, OllamaError
from .protocol import JobState, TERMINAL_STATES
from .skill import install_skill
from .task import DelegationTask
from .triage import DelegationDecision, triage_task
from .worker_plane import WorkerConfig, run_worker_forever, run_worker_once


def _load_eval_runner(repo: Path):
    """Load the evaluator from the declared repository, including console use.

    ``agent-relay`` is installed as a console script, so Python does not necessarily
    put the caller's repository root on ``sys.path``.  The eval suites are
    intentionally repository-local; resolve that root explicitly instead of
    depending on the current interpreter layout.
    """

    repo_root = str(Path(repo).resolve())
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from evals import runner

    return runner


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-relay",
        description="Agent Relay: unified bounded workers and independent verifiers.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="inspect Ollama connectivity")
    doctor.add_argument("--host")
    doctor.add_argument("--model")
    doctor.add_argument("--smoke", action="store_true")
    doctor.add_argument(
        "--codex-smoke",
        action="store_true",
        help="run one disposable Codex CLI local-model capability task",
    )
    doctor.add_argument("--json", action="store_true", dest="as_json")
    doctor.add_argument(
        "--all",
        action="store_true",
        help="probe every registered lane and return truthful readiness states",
    )

    delegate = subparsers.add_parser("delegate", help="run one bounded task")
    delegate.add_argument("--task", required=True, type=Path)
    delegate.add_argument("--repo", type=Path, default=Path.cwd())
    delegate.add_argument(
        "--backend",
        choices=("ollama", "codex-ollama", "local-qwen", "claude-task", "claude-mcp"),
        default="ollama",
    )
    delegate.add_argument("--model")
    delegate.add_argument("--json", action="store_true", dest="as_json")
    delegate.add_argument(
        "--require-triage",
        action="store_true",
        help="fail closed unless the task passes the parent triage gate",
    )
    delegate.add_argument(
        "--allow-untriaged",
        action="store_true",
        help="explicitly bypass the default Codex-backend triage gate for diagnostics",
    )
    delegate.add_argument(
        "--avoided-tokens",
        type=int,
        help="parent estimate used by --require-triage",
    )
    delegate.add_argument(
        "--spent-tokens",
        type=int,
        help="parent estimate used by --require-triage",
    )
    delegate.add_argument(
        "--minimum-leverage",
        type=float,
        default=2.0,
        help="minimum avoided/spent ratio used by --require-triage",
    )
    delegate.add_argument(
        "--compact",
        action="store_true",
        help="print a frontier-facing proof packet without patch text",
    )
    delegate.add_argument(
        "--patch-out",
        type=Path,
        help="write the full patch to this artifact path",
    )

    lanes = subparsers.add_parser("lanes", help="list canonical subagent lanes")
    lanes.add_argument("--json", action="store_true", dest="as_json")
    lanes.add_argument(
        "--check",
        action="store_true",
        help="check local lane prerequisites and configured bridge health",
    )

    serve = subparsers.add_parser("serve", help="run the durable Agent Relay coordinator")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8788)
    serve.add_argument("--db", type=Path, default=default_database())
    serve.add_argument("--token", default=default_auth_token())
    serve.add_argument("--tls-cert", type=Path, help="PEM certificate chain for HTTPS LAN serving")
    serve.add_argument("--tls-key", type=Path, help="PEM private key matching --tls-cert")

    mcp = subparsers.add_parser("mcp", help="run the MCP façade over a durable coordinator")
    mcp.add_argument("--host", default="127.0.0.1")
    mcp.add_argument("--port", type=int, default=8789)
    mcp.add_argument("--coordinator-url", default=os.environ.get("AR_RELAY_COORDINATOR_URL", "http://127.0.0.1:8788"))
    mcp.add_argument("--coordinator-token", default=default_auth_token())
    mcp.add_argument("--token", default=default_auth_token(), help="MCP client bearer token")
    mcp.add_argument("--max-workers", type=int, default=8)
    mcp.add_argument("--request-timeout", type=float, default=30.0)
    mcp.add_argument(
        "--local-worker-backend",
        choices=("local-qwen", "claude-task", "claude-mcp"),
        help="also run a local durable worker so run/Agent can wait for a terminal receipt",
    )
    mcp.add_argument("--local-worker-repo", type=Path, help="checkout used by the optional local worker (defaults to cwd for claude-mcp)")
    mcp.add_argument("--local-worker-id", default="agent-relay-mcp-worker")
    mcp.add_argument("--local-worker-agent-token", default=default_agent_token())
    mcp.add_argument("--local-worker-model")
    mcp.add_argument("--local-worker-lease-seconds", type=float, default=300.0)
    mcp.add_argument("--local-worker-poll-seconds", type=float, default=1.0)

    agents = subparsers.add_parser("agents", help="list or register coordinator agents")
    agents.add_argument("--url", default="http://127.0.0.1:8788")
    agents.add_argument("--token", default=default_auth_token())
    agents.add_argument("--register", type=Path, help="register an Agent Card JSON file")
    agents.add_argument("--revoke", metavar="AGENT_ID", help="revoke one enrolled worker credential")
    agents.add_argument("--task-kind", help="filter cards that support this task kind")
    agents.add_argument("--capability", help="filter cards that advertise this capability")
    agents.add_argument("--readiness", choices=("ready", "degraded", "blocked", "unknown"))
    agents.add_argument("--json", action="store_true", dest="as_json")

    submit = subparsers.add_parser("submit", help="submit one durable bounded task")
    submit.add_argument("--task", required=True, type=Path)
    submit.add_argument("--url", default="http://127.0.0.1:8788")
    submit.add_argument("--token", default=default_auth_token())
    submit.add_argument("--idempotency-key")
    submit.add_argument("--requested-by", default="client")
    submit.add_argument("--workspace-policy-json")
    submit.add_argument("--priority", type=int, default=0)
    submit.add_argument("--deadline-at", help="ISO-8601 deadline with timezone")
    submit.add_argument("--json", action="store_true", dest="as_json")

    inspect = subparsers.add_parser("inspect", help="inspect one durable task")
    inspect.add_argument("task_id")
    inspect.add_argument("--url", default="http://127.0.0.1:8788")
    inspect.add_argument("--token", default=default_auth_token())
    inspect.add_argument("--json", action="store_true", dest="as_json")

    inspect_chain = subparsers.add_parser("inspect-chain", help="inspect one durable follow-up chain")
    inspect_chain.add_argument("chain_id")
    inspect_chain.add_argument("--url", default="http://127.0.0.1:8788")
    inspect_chain.add_argument("--token", default=default_auth_token())
    inspect_chain.add_argument("--json", action="store_true", dest="as_json")

    watch_chain = subparsers.add_parser("watch-chain", help="watch a durable follow-up chain")
    watch_chain.add_argument("chain_id")
    watch_chain.add_argument("--url", default="http://127.0.0.1:8788")
    watch_chain.add_argument("--token", default=default_auth_token())
    watch_chain.add_argument("--interval", type=float, default=1.0)
    watch_chain.add_argument("--timeout", type=float, default=0.0)
    watch_chain.add_argument("--once", action="store_true")
    watch_chain.add_argument("--json", action="store_true", dest="as_json")

    watch = subparsers.add_parser("watch", help="watch one task until a terminal state")
    watch.add_argument("task_id")
    watch.add_argument("--url", default="http://127.0.0.1:8788")
    watch.add_argument("--token", default=default_auth_token())
    watch.add_argument("--interval", type=float, default=1.0)
    watch.add_argument("--timeout", type=float, default=0.0)
    watch.add_argument("--once", action="store_true")
    watch.add_argument("--stream", action="store_true", help="use the bounded SSE event stream")
    watch.add_argument("--json", action="store_true", dest="as_json")

    cancel = subparsers.add_parser("cancel", help="request cancellation of one task")
    cancel.add_argument("task_id")
    cancel.add_argument("--url", default="http://127.0.0.1:8788")
    cancel.add_argument("--token", default=default_auth_token())
    cancel.add_argument("--actor", default="client")
    cancel.add_argument("--json", action="store_true", dest="as_json")

    resume = subparsers.add_parser("resume", help="requeue one waiting task")
    resume.add_argument("task_id")
    resume.add_argument("--url", default="http://127.0.0.1:8788")
    resume.add_argument("--token", default=default_auth_token())
    resume.add_argument("--actor", default="client")
    resume.add_argument("--json", action="store_true", dest="as_json")

    chain_submit = subparsers.add_parser("chain-submit", help="submit one durable follow-up chain step")
    chain_submit.add_argument("--chain-id", required=True)
    chain_submit.add_argument("--step-id", required=True)
    chain_submit.add_argument("--step-index", required=True, type=int)
    chain_submit.add_argument("--task", required=True, type=Path)
    chain_submit.add_argument("--predecessor-task-id")
    chain_submit.add_argument(
        "--allow-predecessor-state",
        action="append",
        dest="allowed_predecessor_states",
        help="terminal predecessor state allowed to unlock this step; repeatable",
    )
    chain_submit.add_argument("--parent-artifact-id", action="append", dest="parent_artifact_ids", default=[])
    chain_submit.add_argument("--parent-message", action="append", dest="parent_messages", default=[])
    chain_submit.add_argument("--url", default="http://127.0.0.1:8788")
    chain_submit.add_argument("--token", default=default_auth_token())
    chain_submit.add_argument("--idempotency-key")
    chain_submit.add_argument("--requested-by", default="orchestrator")
    chain_submit.add_argument("--workspace-policy-json")
    chain_submit.add_argument("--priority", type=int, default=0)
    chain_submit.add_argument("--deadline-at", help="ISO-8601 deadline with timezone")
    chain_submit.add_argument(
        "--defer-until-ready",
        action="store_true",
        help="persist this step and materialize it automatically when the predecessor reaches an allowed terminal state",
    )
    chain_submit.add_argument("--json", action="store_true", dest="as_json")

    worker = subparsers.add_parser("worker", help="run a bounded coordinator worker")
    worker.add_argument("--url", default="http://127.0.0.1:8788")
    worker.add_argument("--token", default=default_auth_token())
    worker.add_argument("--agent-token", default=default_agent_token(), help="scoped credential for this worker")
    worker.add_argument("--worker-id", default="agent-relay-worker")
    worker.add_argument("--repo", type=Path, default=Path.cwd())
    worker.add_argument("--backend", choices=("local-qwen", "claude-task", "claude-mcp"), default="local-qwen")
    worker.add_argument("--model")
    worker.add_argument("--lease-seconds", type=float, default=300.0)
    worker.add_argument("--poll-seconds", type=float, default=2.0)
    worker.add_argument("--once", action="store_true")
    worker.add_argument(
        "--claim-next",
        action="store_true",
        help="ask the coordinator for one highest-priority compatible task instead of listing the queue",
    )
    worker.add_argument("--json", action="store_true", dest="as_json")

    skill = subparsers.add_parser("skill", help="install the bundled Codex skill")
    skill_subparsers = skill.add_subparsers(dest="skill_command", required=True)
    skill_install = skill_subparsers.add_parser(
        "install", help="install Agent Relay into the local Codex skills directory"
    )
    skill_install.add_argument("--destination", type=Path)
    skill_install.add_argument("--archive", type=Path)
    skill_install.add_argument("--force", action="store_true")
    skill_install.add_argument("--json", action="store_true", dest="as_json")

    review = subparsers.add_parser(
        "review",
        help="run the read-only Codex subscription QA verifier",
    )
    review.add_argument("--repo", type=Path, default=Path.cwd())
    review.add_argument("--base")
    review.add_argument("--uncommitted", action="store_true", default=True)
    review.add_argument("--prompt")
    review.add_argument("--model")
    review.add_argument("--reasoning-effort")
    review.add_argument("--codex-bin")
    review.add_argument("--timeout-seconds", type=float)
    review.add_argument("--json", action="store_true", dest="as_json")

    ask = subparsers.add_parser(
        "ask",
        help="consult a specialist lane without accepting its edits as proof",
    )
    ask.add_argument("--lane", choices=("agy-antigravity",), default="agy-antigravity")
    ask.add_argument("--repo", type=Path, default=Path.cwd())
    ask.add_argument("--prompt", required=True)
    ask.add_argument("--model")
    ask.add_argument("--effort")
    ask.add_argument("--mode", choices=("plan", "accept-edits"))
    ask.add_argument("--no-sandbox", action="store_true")
    ask.add_argument("--timeout-seconds", type=float)
    ask.add_argument("--agy-bin")
    ask.add_argument("--json", action="store_true", dest="as_json")

    triage = subparsers.add_parser(
        "triage",
        help="decide whether one task is safe and economical to delegate",
    )
    triage.add_argument("--task", required=True, type=Path)
    triage.add_argument(
        "--avoided-tokens",
        type=int,
        help="parent estimate of Codex tokens avoided by delegating",
    )
    triage.add_argument(
        "--spent-tokens",
        type=int,
        help="parent estimate for triage, decomposition, handoff, review, repair, and recovery",
    )
    triage.add_argument(
        "--minimum-leverage",
        type=float,
        default=2.0,
        help="minimum avoided/spent ratio required for DELEGATE (default: 2.0)",
    )
    triage.add_argument("--max-files", type=int, default=3)
    triage.add_argument("--max-context-items", type=int, default=6)
    triage.add_argument("--json", action="store_true", dest="as_json")

    evaluate = subparsers.add_parser("eval", help="run a declared evaluation suite")
    evaluate.add_argument(
        "--backend",
        choices=("ollama", "codex-ollama", "fixture"),
        default="ollama",
    )
    evaluate.add_argument("--model")
    evaluate.add_argument("--suite", default="bounded-basic")
    evaluate.add_argument("--repo", type=Path, default=Path.cwd())
    evaluate.add_argument("--output", type=Path)
    evaluate.add_argument("--economics", type=Path)
    evaluate.add_argument("--json", action="store_true", dest="as_json")
    evaluate.add_argument(
        "--compact",
        action="store_true",
        help="omit patch/output text and return a frontier proof packet",
    )
    evaluate.add_argument(
        "--aggregate",
        action="store_true",
        help="return counts/index/failures while keeping full evidence as an artifact",
    )
    evaluate.add_argument(
        "--sample",
        type=int,
        default=0,
        help="include this many passing eligible tasks in aggregate review output",
    )
    evaluate.add_argument(
        "--manifest-mode",
        choices=("full", "contract", "thin", "none"),
        default="full",
        help="how much task-definition input to price at the frontier",
    )
    evaluate.add_argument("--artifact-dir", type=Path)
    evaluate.add_argument(
        "--checkpoint",
        type=Path,
        help="write a recoverable full-record checkpoint after each case",
    )
    evaluate.add_argument(
        "--resume",
        action="store_true",
        help="resume the contiguous case prefix stored in --checkpoint",
    )
    evaluate.add_argument(
        "--max-cases",
        type=int,
        help="stop after this many additional cases and leave a resumable checkpoint",
    )

    baseline = subparsers.add_parser(
        "baseline",
        help="run the matched direct-Codex baseline for an evaluation suite",
    )
    baseline.add_argument("--suite", default="bounded-basic")
    baseline.add_argument("--repo", type=Path, default=Path.cwd())
    baseline.add_argument("--model")
    baseline.add_argument(
        "--codex-bin",
        help="direct Codex CLI executable; set this explicitly when PATH is stale",
    )
    baseline.add_argument("--output", type=Path)
    baseline.add_argument("--artifact-dir", type=Path)
    baseline.add_argument("--checkpoint", type=Path)
    baseline.add_argument("--resume", action="store_true")
    baseline.add_argument("--max-cases", type=int)
    baseline.add_argument("--timeout-seconds", type=float, default=180.0)
    baseline.add_argument("--json", action="store_true", dest="as_json")

    batch = subparsers.add_parser(
        "batch",
        help="run independent bounded tasks with one compact frontier handoff",
    )
    batch.add_argument("--manifest", required=True, type=Path)
    batch.add_argument("--repo", type=Path, default=Path.cwd())
    batch.add_argument("--model")
    batch.add_argument("--artifact-dir", type=Path)
    batch.add_argument(
        "--aggregate",
        action="store_true",
        help="return counts/index/failures while retaining full evidence as an artifact",
    )
    batch.add_argument(
        "--sample",
        type=int,
        default=0,
        help="include this many passing tasks in aggregate review output",
    )
    batch.add_argument(
        "--manifest-mode",
        choices=("full", "contract", "thin", "none"),
        default="full",
        help="how much task-definition input to price at the frontier",
    )
    batch.add_argument(
        "--require-triage",
        action="store_true",
        help="fail closed for every manifest task that does not pass parent triage",
    )
    batch.add_argument(
        "--allow-untriaged",
        action="store_true",
        help="explicitly bypass the default batch triage gate for diagnostics",
    )
    batch.add_argument(
        "--avoided-tokens",
        type=int,
        help="global parent estimate used by --require-triage when an entry has none",
    )
    batch.add_argument(
        "--spent-tokens",
        type=int,
        help="global parent estimate used by --require-triage when an entry has none",
    )
    batch.add_argument(
        "--minimum-leverage",
        type=float,
        default=2.0,
        help="minimum avoided/spent ratio used by --require-triage",
    )

    reprice = subparsers.add_parser(
        "reprice",
        help="reprice a recorded run for the compact frontier handoff path",
    )
    reprice.add_argument("--run-report", required=True, type=Path)
    reprice.add_argument("--economics", required=True, type=Path)
    reprice.add_argument("--repo", type=Path, default=Path.cwd())
    reprice.add_argument("--artifact-dir", type=Path)
    reprice.add_argument("--sample", type=int, default=5)
    reprice.add_argument(
        "--manifest-mode",
        choices=("full", "contract", "thin", "none"),
        default="full",
        help="how much task-definition input to price at the frontier",
    )
    reprice.add_argument("--output", type=Path)
    return parser


def _doctor(args: argparse.Namespace) -> int:
    if getattr(args, "all", False):
        payload = {
            "lanes": lane_health_manifest(probe=True),
            "probe": "network_and_local_prerequisites",
            "exit_status_policy": "nonzero only for blocked or degraded lanes",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if all(item["status"] not in {"blocked", "degraded"} for item in payload["lanes"]) else 1

    config = OllamaConfig.from_env()
    if args.host:
        config = OllamaConfig(
            host=args.host,
            default_model=args.model or config.default_model,
            api_key=config.api_key,
            timeout_seconds=config.timeout_seconds,
            temperature=config.temperature,
            num_predict=config.num_predict,
            think=config.think,
            seed=config.seed,
        )
    elif args.model:
        config = OllamaConfig(
            host=config.host,
            default_model=args.model,
            api_key=config.api_key,
            timeout_seconds=config.timeout_seconds,
            temperature=config.temperature,
            num_predict=config.num_predict,
            think=config.think,
            seed=config.seed,
        )

    client = OllamaClient(config)
    report: dict[str, Any] = {
        "host": config.host,
        "configured_model": config.default_model,
        "api_key_configured": bool(config.api_key),
        "models": [],
    }
    try:
        models = client.list_models()
        report["models"] = [
            {"name": item.get("name") or item.get("model"), "digest": item.get("digest")}
            for item in models
        ]
        selected_model = args.model or config.default_model
        if not selected_model and report["models"]:
            selected_model = report["models"][0]["name"]
        if args.smoke:
            generation = client.generate(
                "Reply with exactly OK.",
                "Reply with exactly OK.",
                model=selected_model,
                num_predict=512,
                think=False,
            )
            report["smoke"] = {
                "ok": True,
                "model": generation.model,
                "response": generation.text,
                "duration_seconds": generation.duration_seconds,
            }
        else:
            report["smoke"] = {"ok": None, "hint": "pass --smoke to test generation"}
        if args.codex_smoke:
            report["codex_smoke"] = _codex_smoke(
                model=selected_model,
                host=args.host or None,
            )
            if not report["codex_smoke"].get("ok"):
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 1
        else:
            report["codex_smoke"] = {
                "ok": None,
                "hint": "pass --codex-smoke to test the Codex CLI execution lane",
            }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except OllamaError as exc:
        report["error"] = str(exc)
        if args.as_json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1


def _codex_smoke(*, model: str | None, host: str | None) -> dict[str, Any]:
    """Probe the complete Codex CLI -> Ollama -> patch -> verify path.

    The ordinary doctor smoke only proves that the Ollama HTTP API can answer.
    Codex CLI has an additional provider/protocol boundary, so a successful
    tags or generate request is not enough to authorize a long delegation run.
    The probe uses a temporary one-file repository and a short no-progress
    budget; it never touches the caller's worktree.
    """

    base_config = CodexCliConfig.from_env()
    config = replace(
        base_config,
        default_model=model or base_config.default_model,
        ollama_host=host or base_config.ollama_host,
        # A local model may spend tens of seconds in prompt evaluation before
        # Codex emits its first JSONL event. The smoke must still be bounded,
        # but a 20-second idle cutoff falsely rejects a healthy cold lane.
        timeout_seconds=min(base_config.timeout_seconds, 120.0),
        idle_timeout_seconds=min(base_config.idle_timeout_seconds, 90.0),
    )
    report: dict[str, Any] = {
        "model": config.default_model,
        "provider": config.local_provider,
        "host": config.ollama_host,
        "timeout_seconds": config.timeout_seconds,
        "idle_timeout_seconds": config.idle_timeout_seconds,
    }
    root = Path(tempfile.mkdtemp(prefix="ar-codex-doctor-"))
    try:
        (root / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
        for command in (
            ["init", "--quiet"],
            ["config", "user.name", "Agent Relay Doctor"],
            ["config", "user.email", "agent-relay@example.invalid"],
            ["add", "value.py"],
            ["commit", "--quiet", "-m", "Codex doctor baseline"],
        ):
            completed = subprocess.run(
                ["git", *command],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()
                raise OSError(f"could not initialize Codex smoke repository: {detail[:500]}")
        task = DelegationTask(
            task_id="codex-doctor-smoke",
            objective="Change VALUE from 1 to 2.",
            allowed_files=("value.py",),
            context=("value.py",),
            requirements=("VALUE must equal 2 after the change.",),
            constraints=(
                "Do not touch files outside allowed_files.",
                "Make the smallest valid change.",
            ),
            verification=(
                "python -c \"from value import VALUE; assert VALUE == 2\"",
            ),
            success_criteria=("The declared verification command exits 0.",),
            task_kind="mechanical",
            # The capability probe must exercise the same one-retry recovery
            # contract used by bounded delegations; Qwen may first report a
            # correct summary without a patch candidate when tools are
            # unavailable.
            retry_limit=1,
        )
        started = time.perf_counter()
        result = delegate_local(
            task=task,
            repo=root,
            worker=CodexCliWorker(
                repo=root,
                model=config.default_model,
                config=config,
            ),
        )
        runtime = dict(result.metadata.get("worker_runtime", {}))
        if not runtime:
            history = result.metadata.get("attempt_history", [])
            if isinstance(history, list) and history:
                last_attempt = history[-1]
                if isinstance(last_attempt, dict):
                    candidate = last_attempt.get("local_runtime")
                    if isinstance(candidate, dict):
                        runtime = dict(candidate)
        report.update({
            "ok": result.success,
            "status": result.status.value if hasattr(result.status, "value") else str(result.status),
            "summary": result.summary,
            "duration_seconds": time.perf_counter() - started,
            "attempts": result.attempts,
            "runtime": runtime,
        })
        if not result.success:
            report["error"] = "Codex smoke task did not pass outer verification"
            report["failure_kind"] = runtime.get("failure_kind") or result.status.value
        return report
    except (CodexCliError, OSError, ValueError) as exc:
        report.update({
            "ok": False,
            "error": str(exc),
            "failure_kind": (
                dict(getattr(exc, "runtime", {}) or {}).get("failure_kind")
                or type(exc).__name__
            ),
            "runtime": dict(getattr(exc, "runtime", {}) or {}),
        })
        return report
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _delegate(args: argparse.Namespace) -> int:
    try:
        task_data = json.loads(args.task.read_text(encoding="utf-8"))
        task = DelegationTask.from_dict(task_data)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"status": "WORKER_ERROR", "error": str(exc)}))
        return 2

    triage_result = None
    backend = "codex-ollama" if args.backend == "local-qwen" else args.backend
    triage_required = getattr(args, "require_triage", False) or (
        backend == "codex-ollama"
        and not getattr(args, "allow_untriaged", False)
    )
    if triage_required:
        try:
            triage_result = triage_task(
                task,
                expected_codex_tokens_avoided=getattr(args, "avoided_tokens", None),
                expected_codex_tokens_spent=getattr(args, "spent_tokens", None),
                minimum_leverage=getattr(args, "minimum_leverage", 2.0),
            )
        except ValueError as exc:
            print(json.dumps({"status": "TRIAGE_REJECTED", "error": str(exc)}))
            return 2
        if not triage_result.can_delegate:
            print(json.dumps({
                "status": "TRIAGE_REJECTED",
                "triage": triage_result.to_dict(),
            }, ensure_ascii=False, indent=2))
            return 1 if triage_result.decision is DelegationDecision.KEEP_LOCAL else 2

    if backend == "codex-ollama":
        result = delegate_local(
            task=task,
            repo=args.repo,
            model=args.model,
            worker=CodexCliWorker(repo=args.repo, model=args.model),
        )
    elif backend == "claude-task":
        result = run_claude_task(task, args.repo)
    elif backend == "claude-mcp":
        result = run_claude_mcp_task(task)
    else:
        result = delegate_local(task=task, repo=args.repo, model=args.model)
    patch_artifact = None
    if args.patch_out is not None:
        args.patch_out.parent.mkdir(parents=True, exist_ok=True)
        # Preserve the patch's LF boundaries. Windows text-mode translation
        # turns a valid Git patch into CRLF, which can make the artifact fail
        # when an independent reviewer reapplies it outside the sandbox.
        with args.patch_out.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(result.patch)
        patch_artifact = str(args.patch_out.resolve())
    if args.compact:
        payload = result.to_handoff(patch_artifact=patch_artifact)
    else:
        payload = result.to_dict()
        if patch_artifact is not None:
            payload["patch_artifact"] = patch_artifact
    if triage_result is not None:
        payload["triage"] = triage_result.to_dict()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if result.success else 2


def _lanes(args: argparse.Namespace) -> int:
    checked = getattr(args, "check", False)
    payload = {
        "lanes": lane_health_manifest(probe=True) if checked else lane_manifest()
    }
    if checked:
        payload["probe"] = "network_and_local_prerequisites"
        payload["exit_status_policy"] = "nonzero only for blocked or degraded lanes"
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not checked or all(
        item["status"] not in {"blocked", "degraded"} for item in payload["lanes"]
    ) else 1


def _control_output(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _serve(args: argparse.Namespace) -> int:
    startup = {
        "protocol": "agent-relay/0.3",
        "server": "agent-relay",
        "host": args.host,
        "port": args.port,
        "database": str(args.db.expanduser().resolve()),
        "auth_required": bool(args.token),
        "tls": bool(args.tls_cert or args.tls_key),
    }
    try:
        _control_output(startup)
        serve_forever(
            host=args.host,
            port=args.port,
            database=args.db,
            auth_token=args.token,
            tls_cert=args.tls_cert,
            tls_key=args.tls_key,
        )
        return 0
    except (ControlPlaneError, OSError, ValueError) as exc:
        _control_output({"status": "ERROR", "error": str(exc)})
        return 2


def _mcp(args: argparse.Namespace) -> int:
    startup = {
        "protocol": "agent-relay/0.3",
        "server": "agent-relay-mcp",
        "host": args.host,
        "port": args.port,
        "coordinator_url": args.coordinator_url,
        "auth_required": bool(args.token),
        "max_workers": args.max_workers,
    }
    try:
        local_worker = None
        if args.local_worker_backend:
            if args.local_worker_repo is None and args.local_worker_backend != "claude-mcp":
                raise ValueError("--local-worker-repo is required with --local-worker-backend")
            local_worker = WorkerConfig(
                coordinator_url=args.coordinator_url,
                auth_token=args.coordinator_token,
                agent_token=args.local_worker_agent_token,
                worker_id=args.local_worker_id,
                repo=args.local_worker_repo or Path.cwd(),
                backend=args.local_worker_backend,
                model=args.local_worker_model,
                lease_seconds=args.local_worker_lease_seconds,
                poll_seconds=args.local_worker_poll_seconds,
                claim_next=True,
            )
        _control_output(startup)
        serve_mcp_forever(
            host=args.host,
            port=args.port,
            coordinator_url=args.coordinator_url,
            coordinator_token=args.coordinator_token,
            auth_token=args.token,
            max_workers=args.max_workers,
            request_timeout=args.request_timeout,
            local_worker=local_worker,
        )
        return 0
    except (OSError, ValueError, ControlPlaneError) as exc:
        _control_output({"status": "ERROR", "error": str(exc)})
        return 2


def _control_request(args: argparse.Namespace, method: str, path: str, payload: dict[str, Any] | None = None) -> int:
    try:
        result = request_json(args.url, method, path, payload=payload, auth_token=args.token)
    except ControlPlaneError as exc:
        _control_output({"status": "ERROR", "error": str(exc)})
        return 2
    _control_output(result)
    return 0


def _agents(args: argparse.Namespace) -> int:
    if args.revoke:
        return _control_request(args, "POST", f"/agents/{quote(args.revoke)}/revoke", {})
    if args.register is None:
        filters = []
        if args.task_kind:
            filters.append(f"task_kind={quote(args.task_kind)}")
        if args.capability:
            filters.append(f"capability={quote(args.capability)}")
        if args.readiness:
            filters.append(f"readiness={quote(args.readiness)}")
        path = "/agents" + ("?" + "&".join(filters) if filters else "")
        return _control_request(args, "GET", path)
    try:
        card = json.loads(args.register.read_text(encoding="utf-8"))
        if not isinstance(card, dict):
            raise ValueError("Agent Card must be a JSON object")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _control_output({"status": "ERROR", "error": str(exc)})
        return 2
    return _control_request(args, "POST", "/agents/register", card)


def _submit(args: argparse.Namespace) -> int:
    try:
        task_payload = json.loads(args.task.read_text(encoding="utf-8"))
        if not isinstance(task_payload, dict):
            raise ValueError("task JSON must be an object")
        payload: dict[str, Any] = {"task": task_payload, "requested_by": args.requested_by}
        payload["priority"] = args.priority
        if args.deadline_at:
            payload["deadline_at"] = args.deadline_at
        if args.idempotency_key:
            payload["idempotency_key"] = args.idempotency_key
        if args.workspace_policy_json:
            workspace_policy = json.loads(args.workspace_policy_json)
            if not isinstance(workspace_policy, dict):
                raise ValueError("--workspace-policy-json must be an object")
            payload["workspace_policy"] = workspace_policy
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _control_output({"status": "ERROR", "error": str(exc)})
        return 2
    return _control_request(args, "POST", "/tasks", payload)


def _inspect(args: argparse.Namespace) -> int:
    return _control_request(args, "GET", f"/tasks/{args.task_id}")


def _inspect_chain(args: argparse.Namespace) -> int:
    return _control_request(args, "GET", f"/chains/{quote(args.chain_id)}")


def _chain_terminal(payload: dict[str, Any]) -> bool:
    pending = payload.get("pending_steps", [])
    if not isinstance(pending, list):
        return False
    if any(isinstance(item, dict) and item.get("status") == "pending" for item in pending):
        return False
    steps = payload.get("steps", [])
    if not isinstance(steps, list) or not steps:
        return False
    terminal_values = {item.value for item in TERMINAL_STATES}
    return all(
        isinstance(item, dict)
        and isinstance(item.get("envelope"), dict)
        and item["envelope"].get("state") in terminal_values
        for item in steps
    )


def _watch_chain(args: argparse.Namespace) -> int:
    started = time.monotonic()
    while True:
        try:
            result = request_json(args.url, "GET", f"/chains/{quote(args.chain_id)}", auth_token=args.token)
        except ControlPlaneError as exc:
            _control_output({"status": "ERROR", "error": str(exc)})
            return 2
        _control_output(result)
        if args.once or _chain_terminal(result):
            return 0
        if args.timeout and time.monotonic() - started >= args.timeout:
            _control_output({"status": "TIMEOUT", "chain_id": args.chain_id})
            return 1
        time.sleep(max(0.05, args.interval))


def _watch(args: argparse.Namespace) -> int:
    if args.stream:
        return _watch_stream(args)
    started = time.monotonic()
    while True:
        try:
            result = request_json(args.url, "GET", f"/tasks/{args.task_id}", auth_token=args.token)
        except ControlPlaneError as exc:
            _control_output({"status": "ERROR", "error": str(exc)})
            return 2
        _control_output(result)
        state = result.get("state")
        envelope = result.get("envelope")
        if envelope:
            state = envelope.get("state", state)
        if args.once or state in {item.value for item in TERMINAL_STATES}:
            return 0
        if args.timeout and time.monotonic() - started >= args.timeout:
            _control_output({"status": "TIMEOUT", "task_id": args.task_id})
            return 1
        time.sleep(max(0.05, args.interval))


def _watch_stream(args: argparse.Namespace) -> int:
    started = time.monotonic()
    after = 0
    while True:
        try:
            path = f"/tasks/{args.task_id}/events/stream?after={after}&timeout=30"
            for item in stream_events(args.url, path, auth_token=args.token, timeout=35):
                data = item["data"]
                _control_output(data)
                if item.get("id") is not None:
                    after = int(item["id"])
                state = data.get("state")
                if args.once or state in {entry.value for entry in TERMINAL_STATES}:
                    return 0
        except ControlPlaneError as exc:
            _control_output({"status": "ERROR", "error": str(exc)})
            return 2
        if args.timeout and time.monotonic() - started >= args.timeout:
            _control_output({"status": "TIMEOUT", "task_id": args.task_id})
            return 1


def _cancel(args: argparse.Namespace) -> int:
    return _control_request(args, "POST", f"/tasks/{args.task_id}/cancel", {"actor": args.actor})


def _resume(args: argparse.Namespace) -> int:
    return _control_request(args, "POST", f"/tasks/{args.task_id}/resume", {"actor": args.actor})


def _chain_submit(args: argparse.Namespace) -> int:
    try:
        task_payload = json.loads(args.task.read_text(encoding="utf-8"))
        if not isinstance(task_payload, dict):
            raise ValueError("task JSON must be an object")
        payload: dict[str, Any] = {
            "step_id": args.step_id,
            "step_index": args.step_index,
            "task": task_payload,
            "requested_by": args.requested_by,
            "allowed_predecessor_states": args.allowed_predecessor_states or [JobState.SUCCEEDED.value],
            "parent_artifact_ids": args.parent_artifact_ids,
            "parent_messages": args.parent_messages,
            "defer_until_ready": args.defer_until_ready,
            "priority": args.priority,
        }
        if args.deadline_at:
            payload["deadline_at"] = args.deadline_at
        if args.predecessor_task_id:
            payload["predecessor_task_id"] = args.predecessor_task_id
        if args.idempotency_key:
            payload["idempotency_key"] = args.idempotency_key
        if args.workspace_policy_json:
            workspace_policy = json.loads(args.workspace_policy_json)
            if not isinstance(workspace_policy, dict):
                raise ValueError("--workspace-policy-json must be an object")
            payload["workspace_policy"] = workspace_policy
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _control_output({"status": "ERROR", "error": str(exc)})
        return 2
    return _control_request(args, "POST", f"/chains/{quote(args.chain_id)}/steps", payload)


def _worker(args: argparse.Namespace) -> int:
    try:
        config = WorkerConfig(
            coordinator_url=args.url,
            auth_token=args.token,
            agent_token=args.agent_token,
            worker_id=args.worker_id,
            repo=args.repo,
            backend=args.backend,
            model=args.model,
            lease_seconds=args.lease_seconds,
            poll_seconds=args.poll_seconds,
            claim_next=args.claim_next,
        )
        if args.once:
            outcomes = run_worker_once(config)
            _control_output({"protocol": "agent-relay/0.3", "worker_id": args.worker_id, "outcomes": outcomes})
            return 0 if all(item.get("status") != "worker_error" for item in outcomes) else 1
        run_worker_forever(config)
        return 0
    except (ControlPlaneError, OSError, ValueError) as exc:
        _control_output({"status": "ERROR", "error": str(exc)})
        return 2


def _skill(args: argparse.Namespace) -> int:
    try:
        destination = install_skill(
            destination=args.destination,
            archive=args.archive,
            force=args.force,
        )
    except (OSError, ValueError) as exc:
        payload = {"status": "FAILED", "error": str(exc)}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    payload = {"status": "INSTALLED", "destination": str(destination)}
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Agent Relay skill installed: {destination}")
    return 0


def _review(args: argparse.Namespace) -> int:
    try:
        config = CodexReviewConfig.from_env(
            executable=args.codex_bin,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            timeout_seconds=args.timeout_seconds,
        )
        result = run_codex_review(
            args.repo,
            base=args.base,
            uncommitted=args.uncommitted,
            prompt=args.prompt,
            config=config,
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0 if result.passed else 2
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(json.dumps({
            "lane": "codex-review",
            "status": "FAILED",
            "summary": str(exc),
            "runtime": {"read_only": True},
        }, ensure_ascii=False, indent=2))
        return 2


def _ask(args: argparse.Namespace) -> int:
    try:
        config = AgyConfig.from_env(
            executable=args.agy_bin,
            model=args.model,
            effort=args.effort,
            mode=args.mode,
            sandbox=not args.no_sandbox,
            timeout_seconds=args.timeout_seconds,
        )
        result = run_agy(args.repo, args.prompt, config=config)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0 if result.passed else 2
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(json.dumps({
            "lane": "agy-antigravity",
            "status": "FAILED",
            "summary": str(exc),
            "runtime": {"read_only_default": True},
        }, ensure_ascii=False, indent=2))
        return 2


def _triage(args: argparse.Namespace) -> int:
    try:
        task_data = json.loads(args.task.read_text(encoding="utf-8"))
        task = DelegationTask.from_dict(task_data)
        result = triage_task(
            task,
            expected_codex_tokens_avoided=args.avoided_tokens,
            expected_codex_tokens_spent=args.spent_tokens,
            minimum_leverage=args.minimum_leverage,
            max_allowed_files=args.max_files,
            max_context_items=args.max_context_items,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"decision": "BLOCKED", "error": str(exc)}))
        return 2
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    if result.decision is DelegationDecision.DELEGATE:
        return 0
    if result.decision is DelegationDecision.KEEP_LOCAL:
        return 1
    return 2


def _eval(args: argparse.Namespace) -> int:
    try:
        runner = _load_eval_runner(args.repo)
        report = runner.run_suite(
            backend=args.backend,
            model=args.model,
            suite=args.suite,
            repo_root=args.repo,
            output_path=args.output,
            economics_path=args.economics,
            compact=getattr(args, "compact", False),
            artifact_dir=getattr(args, "artifact_dir", None),
            aggregate=getattr(args, "aggregate", False),
            sample=getattr(args, "sample", 0),
            manifest_mode=getattr(args, "manifest_mode", "full"),
            checkpoint_path=getattr(args, "checkpoint", None),
            resume=getattr(args, "resume", False),
            max_cases=getattr(args, "max_cases", None),
        )
    except Exception as exc:
        print(json.dumps({
            "status": "ERROR",
            "error": f"{type(exc).__name__}: {exc}",
        }))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # A suite can have every case pass while its economic or review gates are
    # still unevaluated. Do not make that look like a successful MVP benchmark.
    return 0 if report.get("mvp_gate", {}).get("overall") == "PASS" else 2


def _baseline(args: argparse.Namespace) -> int:
    try:
        runner = _load_eval_runner(args.repo)
        report = runner.run_codex_baseline_suite(
            suite=args.suite,
            repo_root=args.repo,
            model=args.model,
            codex_bin=args.codex_bin,
            output_path=args.output,
            artifact_dir=args.artifact_dir,
            checkpoint_path=args.checkpoint,
            resume=args.resume,
            max_cases=args.max_cases,
            timeout_seconds=args.timeout_seconds,
        )
    except Exception as exc:
        print(json.dumps({
            "status": "ERROR",
            "error": f"{type(exc).__name__}: {exc}",
        }))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


def _batch(args: argparse.Namespace) -> int:
    try:
        from .batch import run_batch

        report = run_batch(
            manifest=args.manifest,
            repo=args.repo,
            model=args.model,
            artifact_dir=args.artifact_dir,
            aggregate=args.aggregate,
            sample=args.sample,
            manifest_mode=args.manifest_mode,
            require_triage=(
                args.require_triage
                or not getattr(args, "allow_untriaged", False)
            ),
            avoided_tokens=args.avoided_tokens,
            spent_tokens=args.spent_tokens,
            minimum_leverage=args.minimum_leverage,
        )
    except Exception as exc:
        print(json.dumps({
            "status": "ERROR",
            "error": f"{type(exc).__name__}: {exc}",
        }))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 2


def _reprice(args: argparse.Namespace) -> int:
    try:
        runner = _load_eval_runner(args.repo)

        report = runner.reprice_frontier_economics(
            run_report_path=args.run_report,
            economics_path=args.economics,
            repo_root=args.repo,
            artifact_dir=args.artifact_dir,
            sample=args.sample,
            manifest_mode=args.manifest_mode,
            output_path=args.output,
        )
    except Exception as exc:
        print(json.dumps({
            "status": "ERROR",
            "error": f"{type(exc).__name__}: {exc}",
        }))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "ESTIMATED" else 2


def _configure_console_encoding() -> None:
    """Keep bounded remote-agent output printable on Windows consoles."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            # Test capture streams and embedded hosts may not support changing
            # their encoding; the normal print path remains valid there.
            continue


def main(argv: list[str] | None = None) -> int:
    _configure_console_encoding()
    args = _parser().parse_args(argv)
    if args.command == "doctor":
        return _doctor(args)
    if args.command == "delegate":
        return _delegate(args)
    if args.command == "lanes":
        return _lanes(args)
    if args.command == "serve":
        return _serve(args)
    if args.command == "mcp":
        return _mcp(args)
    if args.command == "agents":
        return _agents(args)
    if args.command == "submit":
        return _submit(args)
    if args.command == "inspect":
        return _inspect(args)
    if args.command == "inspect-chain":
        return _inspect_chain(args)
    if args.command == "watch-chain":
        return _watch_chain(args)
    if args.command == "watch":
        return _watch(args)
    if args.command == "cancel":
        return _cancel(args)
    if args.command == "resume":
        return _resume(args)
    if args.command == "chain-submit":
        return _chain_submit(args)
    if args.command == "worker":
        return _worker(args)
    if args.command == "skill":
        return _skill(args)
    if args.command == "review":
        return _review(args)
    if args.command == "ask":
        return _ask(args)
    if args.command == "triage":
        return _triage(args)
    if args.command == "eval":
        return _eval(args)
    if args.command == "baseline":
        return _baseline(args)
    if args.command == "batch":
        return _batch(args)
    if args.command == "reprice":
        return _reprice(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
