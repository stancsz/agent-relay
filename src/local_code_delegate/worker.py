from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Mapping, Protocol

from .ollama import OllamaClient
from .result import WorkerResponse
from .task import DelegationTask


class LocalModel(Protocol):
    def generate(
        self,
        system_prompt: str,
        prompt: str,
        *,
        model: str | None = None,
        json_mode: bool = False,
        think: bool | str | None = None,
    ) -> Any:
        ...


SYSTEM_PROMPT = """You are a bounded implementation worker.
Perform only the task in the contract.
Do not make architecture decisions or unrelated cleanup.
Do not touch files outside allowed_files.
Return one JSON object and nothing else with this shape:
{
  "status": "READY" or "BLOCKED",
  "summary": "short factual summary",
  "patch": "one unified git diff, or complete replacement content when one file is allowed",
  "files": {"relative/path.py": "complete replacement content"},
  "blockers": ["short factual blocker"]
}
The patch must be relative to the clean sandbox baseline. Prefer the files object for
complete file content because it is converted to a checked diff by the supervisor.
If the task says append or add after existing content, return a complete unified
diff for that append. Do not return only the new block in files, because files
content is interpreted as a complete replacement. File content must contain real
line breaks, not literal backslash-n transport text.
When context is a line range or excerpt, prefer a unified diff or only the exact
target definition from that range. A complete-file fallback is allowed only when
every line outside the declared range is preserved exactly; the supervisor maps
it back only after syntax, shape, and changed-line checks. Non-Python ranged tasks
must return a unified diff.
For a ranged Python snippet, the reliable JSON form is:
{"status":"READY","summary":"...","patch":"","files":{"path.py":"exact target definition"},"blockers":[]}
For an insert_after test, the files value must contain only the one or more new
test definitions. Set patch to an empty string when using files; never emit a placeholder
patch such as "- path.py". The returned snippet must be valid executable Python:
replace the old target body rather than leaving an unreachable old return before
the new logic. Read-only context ranges from other files are allowed, but they do
not authorize edits to those files.
Use relative paths only and include only files in allowed_files.
Return either patch or files; do not return both.
Treat the current file context as authoritative: preserve existing functions and
make the smallest necessary edit. Every referenced module or name must be imported
in the edited file, and do not invent classes, APIs, or replacement structure.
Do not emit Markdown fences, comments, or prose outside the JSON object.
Do not include chain-of-thought or a verbose transcript.
If the request is ambiguous, unsafe, or missing required information, return BLOCKED.
"""


@dataclass(frozen=True)
class RetryEvidence:
    previous_patch: str
    verification: tuple[Mapping[str, Any], ...]
    failure_summary: str


def _truncate_retry_text(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    head = max(1, limit - 80)
    return text[:head] + "\n...[retry evidence truncated]"


def _bounded_retry_verification(
    values: tuple[Mapping[str, Any], ...],
) -> list[dict[str, Any]]:
    bounded: list[dict[str, Any]] = []
    for value in values:
        item: dict[str, Any] = {
            key: value.get(key)
            for key in ("command", "exit_code", "duration_seconds", "timed_out", "passed")
            if key in value
        }
        for key in ("stdout", "stderr"):
            if key in value:
                item[key] = _truncate_retry_text(value[key], 2000)
        bounded.append(item)
    return bounded


def build_prompt(
    task: DelegationTask,
    context: str,
    retry: RetryEvidence | None = None,
) -> str:
    ranged_context = any(":" in spec for spec in task.context)
    append_requested = "append" in " ".join(
        (
            task.objective,
            *task.requirements,
            *task.constraints,
            *task.success_criteria,
        )
    ).lower()
    sections = [
        "Implement the bounded coding task below.",
        f"Task ID: {task.task_id}",
        "Objective:\n" + task.objective,
        "Allowed files (you may change only these):\n"
        + "\n".join(f"- {path}" for path in task.allowed_files),
    ]
    if task.context_mode == "insert_after":
        sections.append(
            "Edit mode: insert one or more new test definitions after the declared "
            "context range. For a no-tools response, return only those new test "
            "definitions in files, set patch to an empty string, and preserve the "
            "existing test without imports or unrelated top-level code."
        )
    if append_requested:
        sections.append(
            "Append-only output requirement: return a complete unified diff that "
            "keeps the existing file and adds the requested block. Do not return "
            "only the new block as complete file content."
        )
    if ranged_context:
        sections.append(
            "Ranged-output format requirement: return a valid unified diff, or use "
            "the files object with exactly one replacement entry for the allowed "
            "target file and set patch to an empty string. The files value must be "
            "valid executable Python for the target range; do not return a whole "
            "file, a placeholder patch, or both alternatives."
        )
    if task.requirements:
        sections.append(
            "Requirements:\n" + "\n".join(f"- {item}" for item in task.requirements)
        )
    if task.constraints:
        sections.append(
            "Constraints:\n" + "\n".join(f"- {item}" for item in task.constraints)
        )
    if task.verification:
        sections.append(
            "Verification commands:\n"
            + "\n".join(f"- {item}" for item in task.verification)
        )
    if task.success_criteria:
        sections.append(
            "Success criteria:\n"
            + "\n".join(f"- {item}" for item in task.success_criteria)
        )
    sections.extend([
        "Current bounded file context:",
        context,
        "Produce the smallest correct change. Do not restate this task or explain your reasoning.",
        "Final acceptance checklist (verify every item before responding):\n"
        + "\n".join(f"- {item}" for item in (
            *task.requirements,
            *task.constraints,
            *task.success_criteria,
        )),
        "Return exactly the JSON object requested by the system prompt.",
    ])
    if retry is not None:
        retry_instruction = (
            "This is a retry against the same clean baseline. The previous candidate "
            "and its verification failure are evidence. Return a minimal unified diff "
            "or only the exact target-definition snippet for the declared range; do "
            "not return a complete replacement file because the context was a line "
            "range or excerpt."
            if ranged_context
            else
            "This is a retry against the same clean baseline. The previous candidate "
            "and its verification failure are evidence. Return the smallest corrected "
            "unified diff possible. If you use the files object, preserve every "
            "unrelated definition from the provided complete file."
        )
        sections.extend([
            retry_instruction,
            "Previous candidate (patch or files object):\n"
            + _truncate_retry_text(retry.previous_patch, 12000),
            "Verification failure:\n" + _truncate_retry_text(retry.failure_summary, 2000),
            "Verification details:\n" + json.dumps(
                _bounded_retry_verification(retry.verification),
                ensure_ascii=False,
                indent=2,
            ),
        ])
    return "\n\n".join(sections)


def _candidate_json(text: str) -> Mapping[str, Any] | None:
    stripped = text.strip()
    candidates = [stripped]
    if stripped.startswith("~~~") and stripped.endswith("~~~"):
        lines = stripped.splitlines()
        candidates.append("\n".join(lines[1:-1]).strip())
    fence = chr(96) * 3
    if fence in stripped:
        lines = stripped.splitlines()
        candidates.append("\n".join(
            line for line in lines if not line.strip().startswith(fence)
        ).strip())

    def load_object(candidate: str) -> Mapping[str, Any] | None:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, Mapping):
            return value
        return None

    # Small local models sometimes stop immediately after closing the files
    # object, leaving only the outer response object unclosed. Recover only
    # balanced, end-of-output truncation; never invent missing field content.
    def close_unclosed_containers(candidate: str) -> str | None:
        stack: list[str] = []
        in_string = False
        escaped = False
        for char in candidate:
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char in "[{":
                stack.append(char)
            elif char in "]}":
                if not stack or (char == "]" and stack[-1] != "[") or (char == "}" and stack[-1] != "{"):
                    return None
                stack.pop()
        if in_string or not stack:
            return None
        return candidate + "".join("}" if item == "{" else "]" for item in reversed(stack))

    for candidate in candidates:
        value = load_object(candidate)
        if value is not None:
            return value
        repaired = close_unclosed_containers(candidate)
        if repaired is not None:
            value = load_object(repaired)
            if value is not None:
                return value

    decoder = json.JSONDecoder()
    expected_keys = {"status", "summary", "patch", "files", "file_contents", "blockers"}
    for candidate in candidates:
        try:
            value, end = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            value = None
            end = 0
        if (
            isinstance(value, Mapping)
            and expected_keys.intersection(value)
            and not candidate[end:].strip()
        ):
            return value
        for index, char in enumerate(candidate):
            if char != "{":
                continue
            try:
                value, end = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                continue
            if (
                isinstance(value, Mapping)
                and expected_keys.intersection(value)
                and not candidate[index + end :].strip()
            ):
                return value
    return None


def parse_worker_response(
    text: str,
    *,
    prefer_patch: bool = False,
) -> WorkerResponse:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("worker returned an empty response")

    value = _candidate_json(text)
    if value is None:
        stripped = text.strip()
        if stripped.startswith("diff --git ") or stripped.startswith("--- "):
            return WorkerResponse(
                status="READY",
                summary="Worker returned a raw unified diff.",
                patch=stripped,
                raw_response=text,
            )
        raise ValueError("worker response was neither JSON nor a unified diff")

    status = str(value.get("status", "READY")).upper()
    summary = str(value.get("summary", "")).strip()
    patch = value.get("patch", "")
    if patch is None:
        patch = ""
    if not isinstance(patch, str):
        raise ValueError("worker patch must be a string")
    blockers_value = value.get("blockers", [])
    if isinstance(blockers_value, str):
        blockers = (blockers_value,)
    elif isinstance(blockers_value, list):
        blockers = tuple(str(item) for item in blockers_value)
    else:
        raise ValueError("worker blockers must be a list or string")
    if status == "BLOCKED":
        return WorkerResponse(
            status="BLOCKED",
            summary=summary or "Worker reported that the task is blocked.",
            blockers=blockers,
            raw_response=text,
        )
    if status not in {"READY", "SUCCESS"}:
        raise ValueError(f"unsupported worker status: {status}")
    files_value = value.get("files")
    if not files_value:
        files_value = value.get("file_contents", {})
    file_contents: list[tuple[str, str]] = []
    if files_value:
        if not isinstance(files_value, Mapping):
            raise ValueError("worker files must be an object mapping paths to content")
        for path, content in files_value.items():
            if not isinstance(path, str) or not isinstance(content, str):
                raise ValueError("worker file paths and contents must be strings")
            file_contents.append((path, content))
        # Direct Ollama keeps the historical files-first behavior. The Codex
        # adapter can request both candidates so it can test which one applies
        # to the clean sandbox before selecting an authoritative patch.
        if not prefer_patch:
            patch = ""
    return WorkerResponse(
        status="READY",
        summary=summary,
        patch=patch,
        blockers=blockers,
        file_contents=tuple(file_contents),
        raw_response=text,
    )


class OllamaWorker:
    def __init__(self, client: OllamaClient, model: str | None = None) -> None:
        self.client = client
        self.model = model

    def run(
        self,
        task: DelegationTask,
        context: str,
        retry: RetryEvidence | None = None,
    ) -> WorkerResponse:
        response = self.client.generate(
            SYSTEM_PROMPT,
            build_prompt(task, context, retry),
            model=self.model or task.model,
            json_mode=True,
            think=None,
        )
        text = getattr(response, "text", response)
        parsed = parse_worker_response(text)
        raw = getattr(response, "raw", {})
        runtime: dict[str, Any] = {
            "model": getattr(response, "model", self.model or task.model),
            "wall_clock_seconds": getattr(response, "duration_seconds", None),
        }
        if isinstance(raw, Mapping):
            for key in (
                "total_duration",
                "load_duration",
                "prompt_eval_count",
                "prompt_eval_duration",
                "eval_count",
                "eval_duration",
                "done_reason",
            ):
                if key in raw:
                    runtime[key] = raw[key]
        return replace(parsed, runtime=runtime)
