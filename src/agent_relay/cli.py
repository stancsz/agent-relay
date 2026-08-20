from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

from .codex_worker import CodexCliConfig, CodexCliError, CodexCliWorker
from .codex_review import CodexReviewConfig, run_codex_review
from .agy_antigravity import AgyConfig, run_agy
from .delegate import delegate_local
from .lanes import canonical_lane_name, lane_manifest
from .ollama import OllamaClient, OllamaConfig, OllamaError
from .task import DelegationTask
from .triage import DelegationDecision, triage_task


def _load_eval_runner(repo: Path):
    """Load the evaluator from the declared repository, including console use.

    ``lcd`` is installed as a console script, so Python does not necessarily
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

    delegate = subparsers.add_parser("delegate", help="run one bounded task")
    delegate.add_argument("--task", required=True, type=Path)
    delegate.add_argument("--repo", type=Path, default=Path.cwd())
    delegate.add_argument(
        "--backend",
        choices=("ollama", "codex-ollama", "local-qwen"),
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
    root = Path(tempfile.mkdtemp(prefix="lcd-codex-doctor-"))
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
    payload = {"lanes": lane_manifest()}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "doctor":
        return _doctor(args)
    if args.command == "delegate":
        return _delegate(args)
    if args.command == "lanes":
        return _lanes(args)
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
