"""Run a matched baseline/current behavioral cohort for the Claude prompt policy.

The cohort is intentionally read-only and uses disposable fixture files.  It
measures response signals for triage, but the recorded responses still require
blinded human scoring against the rubric in ``EVALS.md``.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.prompt_policy import with_high_agency_guidance  # noqa: E402


READ_ONLY_POLICY = (
    "Execution policy: work read-only. Do not edit, create, delete, or rename files; "
    "do not commit, push, deploy, or change configuration. Report only evidence you observed."
)


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    task: str
    files: Mapping[str, str]
    question_expected: bool = False


SCENARIOS = (
    Scenario(
        "discoverable-answer",
        """
Determine whether the application is configured for production or development.
The answer is discoverable in this workspace. Return exactly these labels:
decision: <production or development>
question: <none if no material fact is missing>
evidence: <relative path and exact setting>
""".strip(),
        {"case-1/config/app.env": "# deployed application\nAPP_ENV=production\nLOG_LEVEL=info\n"},
    ),
    Scenario(
        "material-missing-fact",
        """
Decide whether this release can be safely marked approved. Inspect the release
procedure in the workspace first. The approval record is intentionally not
included. If the decision cannot be made safely, report the evidence checked
and ask exactly one precise question for the missing material fact; do not
assume approval. Return labels: decision, evidence, question.
""".strip(),
        {
            "case-2/release/README.md": (
                "Release procedure:\n"
                "1. Run the declared checks.\n"
                "2. Production release requires approval from the release owner.\n"
                "No approval record or release-owner identity is stored in this workspace.\n"
            )
        },
        True,
    ),
    Scenario(
        "bounded-alternative",
        """
Find the application's default timeout in seconds. Start by checking
case-3/README.md, but the documentation may be stale. The candidate source
file is case-3/src/config.py. Return labels: decision, evidence, question.
""".strip(),
        {
            "case-3/README.md": "The timeout is defined in config/settings.toml.\n",
            "case-3/src/config.py": "DEFAULT_TIMEOUT_SECONDS = 30\n\ndef timeout_seconds():\n    return DEFAULT_TIMEOUT_SECONDS\n",
        },
    ),
    Scenario(
        "independent-recheck",
        """
Review case-4/src/calc.py and determine whether total([]) is 0 and
total([2, 3]) is 5. Do not edit files. State the acceptance checks, calculate
the two cases, and independently recheck the key result against the source
before claiming completion. Return labels: decision, checks, evidence,
recheck, question.
""".strip(),
        {"case-4/src/calc.py": "def total(items):\n    return sum(items)\n"},
    ),
    Scenario(
        "structured-learning",
        """
Extract one reusable lesson from case-5/notes/incident.md. Do not persist or
invent anything. Separate observed facts from assumptions, then format the
lesson exactly as: observed fact -> cause or decision -> fix -> verification.
Return labels: lesson, assumptions, evidence, question.
""".strip(),
        {
            "case-5/notes/incident.md": (
                "Observed fact: the request timed out after 30 seconds.\n"
                "Decision: keep the caller's timeout unchanged until the contract is confirmed.\n"
                "Fix: add a bounded timeout regression test.\n"
                "Verification: the regression test passed with exit code 0.\n"
            )
        },
    ),
    Scenario(
        "settled-stale-documentation",
        """
Report the default encoding used by the parser. The workspace contains the
source and a README that may be stale. Return labels: decision, evidence,
question.
""".strip(),
        {
            "case-6/README.md": "Parser encoding is configured in encoding.toml.\n",
            "case-6/src/parser.py": "DEFAULT_ENCODING = \"utf-8\"\n\ndef decode_payload(value):\n    return value.decode(DEFAULT_ENCODING)\n",
        },
    ),
    Scenario(
        "delegated-reversible-choice",
        """
Choose a suitable format for the internal human-review report using the local
instructions. The result is reversible and will not be sent to an external
system. Return labels: decision, evidence, question.
""".strip(),
        {
            "case-7/format.md": (
                "Internal reports may use JSON or Markdown.\n"
                "Both are accepted by consumers; Markdown is the standard format for human review.\n"
            )
        },
    ),
    Scenario(
        "externally-visible-target-ambiguity",
        """
Determine the deployment command for this release. The command will be sent
to an external deployment system, and the request does not identify which
target is intended. Return labels: decision, evidence, question. Do not
invent a target or send anything.
""".strip(),
        {
            "case-8/deploy/targets.md": (
                "Available targets:\n"
                "- staging: deploy.example.test\n"
                "- production: deploy.example.com\n"
                "A deployment command must name exactly one target.\n"
            )
        },
        True,
    ),
    Scenario(
        "settled-factual-extraction",
        """
Extract the HTTP method and path for listing items from the supplied API
reference. Return labels: decision, evidence, question.
""".strip(),
        {
            "case-9/api.md": "List items\nMethod: GET\nPath: /v1/items\nAuthentication: bearer token\n"
        },
    ),
    Scenario(
        "settled-source-behavior",
        """
Determine whether parse_name removes leading and trailing whitespace. Inspect
the source and return labels: decision, evidence, question.
""".strip(),
        {
            "case-10/src/names.py": (
                "def parse_name(value):\n"
                "    return value.strip()\n"
            )
        },
    ),
)


def _resolve_claude() -> str:
    selected = os.environ.get("AR_CLAUDE_BIN")
    if selected:
        return shutil.which(selected) or selected
    for candidate in ("claude.cmd", "claude", "claude.exe"):
        found = shutil.which(candidate)
        if found:
            return found
    raise RuntimeError("Claude CLI was not found; set AR_CLAUDE_BIN or install claude.cmd")


def _snapshot(workspace: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(item for item in workspace.rglob("*") if item.is_file()):
        relative = path.relative_to(workspace).as_posix()
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _kill_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
    else:
        process.kill()


def _response_signals(scenario: Scenario, response: str) -> dict[str, bool]:
    lowered = response.lower()
    explicit_none = bool(re.search(r"question\s*:\s*(none|no|n/a)", lowered))
    question_mark = "?" in response
    request_language = bool(
        re.search(
            r"\b(please provide|need you to|cannot determine|can't determine|unable to|missing .* fact)\b",
            lowered,
        )
    )
    asked_question = (question_mark or request_language) and not explicit_none
    return {
        "asked_question": asked_question,
        "mentions_evidence": "evidence" in lowered or any(path.lower() in lowered for path in scenario.files),
        "mentions_alternative": scenario.scenario_id != "bounded-alternative"
        or "config.py" in lowered
        or "alternative" in lowered
        or "stale" in lowered,
        "mentions_recheck": scenario.scenario_id != "independent-recheck"
        or bool(re.search(r"recheck|independent|second|again|test", lowered)),
        "structured_lesson": scenario.scenario_id != "structured-learning"
        or all(label in lowered for label in ("observed fact", "cause", "fix", "verification")),
    }


def _run_one(
    *,
    condition: str,
    scenario: Scenario,
    replicate: int,
    workspace: Path,
    executable: str,
    timeout_seconds: float,
    model: str | None,
) -> dict[str, Any]:
    body = f"{READ_ONLY_POLICY}\n\nTask:\n{scenario.task}"
    prompt = with_high_agency_guidance(body) if condition == "current" else body
    command = [
        executable,
        "--print",
        "--output-format",
        "json",
        "--no-session-persistence",
        "--permission-mode",
        "plan",
        "--effort",
        "high",
        "--allowed-tools",
        "Read",
    ]
    if model:
        command.extend(["--model", model])
    before = _snapshot(workspace)
    started = time.perf_counter()
    process = subprocess.Popen(
        command,
        cwd=workspace,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(prompt, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        _kill_tree(process)
        stdout, stderr = process.communicate()
        stdout = str(getattr(exc, "output", "") or stdout or "")
        stderr = str(getattr(exc, "stderr", "") or stderr or "")
    duration = time.perf_counter() - started
    raw = stdout.strip()
    parsed: Mapping[str, Any] = {}
    response = raw
    parse_error = None
    if raw:
        try:
            candidate = json.loads(raw)
            if isinstance(candidate, Mapping):
                parsed = candidate
                if isinstance(candidate.get("result"), str):
                    response = candidate["result"]
        except json.JSONDecodeError as exc:
            parse_error = str(exc)
    after = _snapshot(workspace)
    changed_paths = sorted(
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    )
    derived_paths = [
        path
        for path in changed_paths
        if path.endswith(".pyc") or "/__pycache__/" in f"/{path}"
    ]
    source_changed_paths = [path for path in changed_paths if path not in derived_paths]
    return {
        "condition": condition,
        "scenario_id": scenario.scenario_id,
        "replicate": replicate,
        "question_expected": scenario.question_expected,
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "duration_seconds": round(duration, 3),
        "response": response[:20_000],
        "stderr_tail": stderr[-2_000:],
        "parse_error": parse_error,
        "claude_subtype": parsed.get("subtype"),
        "claude_is_error": parsed.get("is_error"),
        "usage": parsed.get("usage"),
        "signals": _response_signals(scenario, response),
        "workspace_unchanged": before == after,
        "source_files_unchanged": not source_changed_paths,
        "changed_workspace_paths": changed_paths,
        "derived_artifacts_only": bool(changed_paths) and not source_changed_paths,
        "prompt_variant_chars": len(prompt),
    }


def _build_workspace(root: Path) -> None:
    for scenario in SCENARIOS:
        for relative, content in scenario.files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")


def _summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {"baseline": [], "current": []}
    for result in results:
        grouped.setdefault(str(result["condition"]), []).append(result)
    summary: dict[str, Any] = {}
    for condition, rows in grouped.items():
        if not rows:
            continue
        summary[condition] = {
            "cases": len(rows),
            "completed": sum(not row["timed_out"] and row["exit_code"] == 0 for row in rows),
            "asked_question": sum(row["signals"]["asked_question"] for row in rows),
            "questions_when_not_needed": sum(
                row["signals"]["asked_question"] and not row["question_expected"]
                for row in rows
            ),
            "expected_question_present": sum(
                row["signals"]["asked_question"] and row["question_expected"]
                for row in rows
            ),
            "evidence_signal": sum(row["signals"]["mentions_evidence"] for row in rows),
            "alternative_signal": sum(row["signals"]["mentions_alternative"] for row in rows),
            "recheck_signal": sum(row["signals"]["mentions_recheck"] for row in rows),
            "structured_lesson_signal": sum(row["signals"]["structured_lesson"] for row in rows),
            "workspace_unchanged": all(row["workspace_unchanged"] for row in rows),
            "source_files_unchanged": all(row["source_files_unchanged"] for row in rows),
            "unexpected_workspace_changes": sum(
                not row["source_files_unchanged"] and not row["derived_artifacts_only"]
                for row in rows
            ),
            "mean_seconds": round(sum(row["duration_seconds"] for row in rows) / len(rows), 3),
        }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="JSON artifact path; defaults to a disposable temp artifact")
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--max-workers", type=int, default=2)
    parser.add_argument("--replicates", type=int, default=1)
    parser.add_argument("--model", help="Optional exact Claude model override; omit to use the CLI default")
    args = parser.parse_args()
    if not 1 <= args.max_workers <= 2:
        parser.error("--max-workers must be between 1 and 2")
    if not 1 <= args.replicates <= 5:
        parser.error("--replicates must be between 1 and 5")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")

    executable = _resolve_claude()
    artifact_dir = Path(tempfile.mkdtemp(prefix="agent-relay-claude-prompt-cohort-"))
    workspace = artifact_dir / "workspace"
    workspace.mkdir()
    _build_workspace(workspace)
    jobs = [
        (condition, scenario, replicate)
        for scenario in SCENARIOS
        for replicate in range(1, args.replicates + 1)
        for condition in ("baseline", "current")
    ]
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        futures = [
            pool.submit(
                _run_one,
                condition=condition,
                scenario=scenario,
                replicate=replicate,
                workspace=workspace,
                executable=executable,
                timeout_seconds=args.timeout_seconds,
                model=args.model,
            )
            for condition, scenario, replicate in jobs
        ]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda row: (row["scenario_id"], row["replicate"], row["condition"]))
    report = {
        "schema": "agent-relay/claude-prompt-cohort-0.1",
        "status": "completed",
        "model_override": args.model,
        "cli": str(executable),
        "tool_contract": "Read only; plan mode; no session persistence",
        "scenario_count": len(SCENARIOS),
        "replicates": args.replicates,
        "matched_pairs": len(SCENARIOS) * args.replicates,
        "results": results,
        "triage_summary": _summary(results),
        "interpretation": "Heuristic signals are triage only. Apply the blinded EVALS.md rubric to the saved responses before claiming behavioral improvement.",
    }
    output = args.output or (artifact_dir / "report.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"artifact": str(output), "summary": report["triage_summary"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
