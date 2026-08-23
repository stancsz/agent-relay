from __future__ import annotations

from dataclasses import dataclass, replace
import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Mapping

from .patch import (
    append_diff,
    append_hunk_diff,
    PatchError,
    capture_diff,
    check_patch,
    ranged_full_file_diff,
    ranged_replacement_diff,
    replacement_diff,
)
from .codex_proxy import OllamaCompatProxy
from .ollama import (
    DEFAULT_OLLAMA_MODEL,
    OllamaClient,
    OllamaConfig,
    OllamaError,
)
from .env import load_dotenv
from .result import WorkerResponse
from .sandbox import GitSandbox, SandboxError
from .task import DelegationTask, context_path_and_range, normalize_relative_path
from .worker import (
    RetryEvidence,
    _bounded_retry_verification,
    _truncate_retry_text,
    parse_worker_response,
)


class CodexCliError(RuntimeError):
    """Raised when Codex CLI cannot produce a bounded local-model result."""

    def __init__(
        self,
        message: str,
        *,
        timed_out: bool = False,
        retryable: bool = False,
        runtime: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.timed_out = timed_out
        self.retryable = retryable
        self.runtime = dict(runtime or {})


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _env_int(name: str, default: int | None) -> int | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _default_executable() -> str:
    if os.name == "nt":
        return shutil.which("codex.cmd") or "codex.cmd"
    return shutil.which("codex") or "codex"


@dataclass(frozen=True)
class CodexCliConfig:
    executable: str = "codex"
    # Kept as a provider label for preflight and telemetry. The worker uses a
    # temporary custom Codex provider instead of the legacy --oss flags.
    local_provider: str = "ollama-chat"
    provider_id: str = "ar-ollama"
    # Current Codex CLI releases require the Responses wire API. Older Codex
    # releases accepted ``chat``; keep that value as an explicit compatibility
    # override rather than making it the default for a fresh install.
    wire_api: str = "responses"
    default_model: str = DEFAULT_OLLAMA_MODEL
    # Bounded mechanical tasks should default to the cheapest reliable
    # reasoning lane. Callers can opt into a stronger pass explicitly, and the
    # optional retry model remains the escalation path for a failed attempt.
    reasoning_effort: str = "low"
    # The child runs inside a disposable Git worktree. Current Codex CLI
    # rejects non-interactive shell execution under ``workspace-write`` with
    # approval_policy=never, so the inner CLI must use full access while the
    # outer supervisor remains the scope/apply/verification authority.
    sandbox: str = "danger-full-access"
    ollama_host: str = "http://localhost:11434"
    timeout_seconds: float = 180.0
    # A model that emits only the initial Codex thread events is not making
    # useful progress. Fail that lane before consuming the full task timeout.
    idle_timeout_seconds: float = 90.0
    require_model_present: bool = True
    probe_version: bool = True
    output_schema: bool = False
    retry_model: str | None = None
    compat_proxy_enabled: bool = True
    disable_reasoning: bool = True
    strip_tools: bool = True
    compact_prompt: bool = True
    # Keep bounded coding tasks away from the model's potentially enormous
    # provider default (Qwen3.5 can advertise a 262k context).  This is a
    # resource/reliability bound, not a token-savings measurement.
    ollama_num_ctx: int | None = 8192
    ollama_num_predict: int | None = None
    ollama_temperature: float | None = 0.0
    ollama_seed: int | None = None

    @classmethod
    def from_env(cls) -> "CodexCliConfig":
        load_dotenv()
        return cls(
            executable=os.environ.get("AR_CODEX_BIN", _default_executable()),
            local_provider=os.environ.get(
                "AR_CODEX_LOCAL_PROVIDER", "ollama-chat"
            ),
            provider_id=os.environ.get("AR_CODEX_PROVIDER_ID", "ar-ollama"),
            wire_api=os.environ.get("AR_CODEX_WIRE_API", "responses"),
            default_model=os.environ.get(
                "AR_CODEX_MODEL", DEFAULT_OLLAMA_MODEL
            ),
            reasoning_effort=os.environ.get(
                "AR_CODEX_REASONING_EFFORT", "low"
            ),
            sandbox=os.environ.get(
                "AR_CODEX_SANDBOX", "danger-full-access"
            ),
            ollama_host=os.environ.get(
                "AR_CODEX_OLLAMA_HOST", "http://localhost:11434"
            ),
            timeout_seconds=_env_float("AR_CODEX_TIMEOUT_SECONDS", 180.0),
            idle_timeout_seconds=_env_float(
                "AR_CODEX_IDLE_TIMEOUT_SECONDS", 90.0
            ),
            require_model_present=_env_bool(
                "AR_CODEX_REQUIRE_MODEL_PRESENT", True
            ),
            probe_version=_env_bool("AR_CODEX_PROBE_VERSION", True),
            output_schema=_env_bool("AR_CODEX_OUTPUT_SCHEMA", False),
            retry_model=(os.environ.get("AR_CODEX_RETRY_MODEL") or None),
            compat_proxy_enabled=_env_bool("AR_CODEX_COMPAT_PROXY", True),
            disable_reasoning=_env_bool(
                "AR_CODEX_DISABLE_REASONING", True
            ),
            strip_tools=_env_bool("AR_CODEX_STRIP_TOOLS", True),
            compact_prompt=_env_bool("AR_CODEX_COMPACT_PROMPT", True),
            ollama_num_ctx=_env_int("AR_CODEX_NUM_CTX", 8192),
            ollama_num_predict=_env_int("AR_CODEX_NUM_PREDICT", None),
            ollama_temperature=_env_float("AR_CODEX_TEMPERATURE", 0.0),
            ollama_seed=_env_int("AR_CODEX_SEED", None),
        )


CODEX_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "summary", "patch", "files", "blockers"],
    "properties": {
        "status": {"type": "string", "enum": ["READY", "BLOCKED"]},
        "summary": {"type": "string", "maxLength": 500},
        "patch": {"type": "string"},
        "files": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
        "blockers": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}


def build_codex_prompt(
    task: DelegationTask,
    context: str,
    retry: RetryEvidence | None = None,
) -> str:
    """Build the bounded execution contract sent to Codex's local-model loop."""

    sections = [
        "You are the execution harness for one bounded local coding task.",
        "Work directly in the current disposable Git sandbox using your tools.",
        "Implement only the task below. Do not make architecture decisions,"
        " unrelated cleanup, commits, or changes outside allowed_files.",
        "You may inspect repository files read-only when needed, but you may"
        " write only the explicitly allowed files.",
        f"Task ID: {task.task_id}",
        "Objective:\n" + task.objective,
            "Allowed files (write scope):\n"
            + "\n".join(f"- {path}" for path in task.allowed_files),
    ]
    if task.context_mode == "insert_after":
        sections.append(
            "Edit mode: insert one or more new tests after the declared context "
            "range and preserve the existing test. In a no-tools response, put "
            "only those new test definitions in files, set patch to an empty string, "
            "and do not include imports, Markdown, or unrelated top-level code."
        )
    if len(task.allowed_files) == 1 and any(":" in spec for spec in task.context):
        sections.append(
            "Ranged single-file compatibility rule: return only the exact complete "
            "target definition or snippet for the declared range in files and set "
            "patch to an empty string. Do not return the whole file, a line-number "
            "fragment, or a diff; the supervisor will place this valid Python "
            "snippet back into the declared range."
        )
    elif len(task.allowed_files) == 1:
        sections.append(
            "Single-file compatibility rule: when tools are unavailable, return "
            "the complete current content of the one allowed file in files and set "
            "patch to an empty string. Preserve every unchanged line exactly; do "
            "not return a partial file or a unified diff for this one-file fallback."
        )
    sections.append(
        "Preservation rule: when the requirements or constraints say preserve, "
        "append, or modify only one section, keep every existing line outside "
        "that change byte-for-byte. For a no-tools fallback, prefer a minimal "
        "unified diff for append-only edits. For one small file, prefer complete "
        "current content in files when diff syntax is uncertain. Any files content "
        "must contain real line breaks, not literal backslash-n text."
    )
    sections.append(
        "Execution budget: inspect only the listed context and necessary symbols, "
        "make one focused edit pass, run each declared verification command once, "
        "then stop. Do not narrate a plan, repeat the context, expose reasoning, "
        "or start unrelated cleanup."
    )
    if _append_only_requested(task):
        sections.append(
            "Append-only output rule: preserve the existing content. In a one-file "
            "no-tools compatibility lane, return the complete resulting file in "
            "files; otherwise return a complete unified diff for the append. Never "
            "return only the new block as complete file content, because that would "
            "replace the existing file."
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
            "Verification commands: run every command before reporting READY.\n"
            + "\n".join(f"- {item}" for item in task.verification)
        )
    if task.success_criteria:
        sections.append(
            "Success criteria:\n"
            + "\n".join(f"- {item}" for item in task.success_criteria)
        )
    sections.extend(
        [
            "Required starting context:\n" + context,
            "If the task is ambiguous, unsafe, missing required information, or "
            "requires unavailable credentials, do not edit files and report BLOCKED.",
            "The supervisor captures the Git diff after you finish. Prefer using "
            "your tools to edit the sandbox and run the declared checks when tool "
            "execution is responsive; if a tool edit succeeds, keep the final JSON "
            "minimal with patch set to an empty string and files set to an empty "
            "object. If your runtime cannot use tools or they are unresponsive, "
            "return complete replacement content for each changed allowed file in "
            "the optional files object instead, or a complete unified patch. Do not "
            "claim READY without either a sandbox diff or a complete files/patch "
            "candidate.",
            "Candidate file paths must exactly match allowed_files, including the "
            "directory and extension. Never shorten config.py to config or invent "
            "a different path.",
            "When returning patch, use a standard unified diff: start with "
            "diff --git a/path b/path, then --- a/path and +++ b/path, followed by "
            "a valid @@ hunk. Do not omit the space after +++ or emit fake index "
            "metadata. If you cannot produce that exact format, use complete files "
            "content instead.",
            "For ranged tasks, prefer the exact target definition or a complete "
            "unified diff. A complete-file fallback is allowed only when every "
            "line outside the declared range is preserved exactly; the outer "
            "verifier will reject broader changes.",
            "At the end, return exactly one JSON object and no Markdown or prose:",
            '{"status":"READY" or "BLOCKED", "summary":"short factual result", '
            '"patch":"", "files":{}, "blockers":[]}',
        ]
    )
    if retry is not None:
        if len(task.allowed_files) == 1 and not any(
            ":" in spec for spec in task.context
        ):
            retry_output_rule = (
                "Recovery output rule: if the sandbox diff is not captured, return "
                "the complete current content of the one allowed file in the files "
                "object and set patch to an empty string. Do not return a partial, "
                "line-number-only, or guessed unified patch."
            )
        elif len(task.allowed_files) == 1:
            if task.context_mode == "insert_after":
                retry_output_rule = (
                    "Recovery output rule: for this insert_after task, return only "
                    "the one new valid test definition in the files object and set "
                    "patch to an empty string, or return a complete unified diff. "
                    "Do not include imports, the existing test, or a guessed "
                    "line-number-only patch."
                )
            else:
                retry_output_rule = (
                    "Recovery output rule: for this ranged single-file task, return "
                    "the exact valid target definition in the files object and set "
                    "patch to an empty string, or return a complete unified diff "
                    "with valid file headers. Do not return a guessed "
                    "line-number-only patch."
                )
        else:
            retry_output_rule = (
                "Recovery output rule: return one complete unified diff with valid "
                "file headers, or complete files content for every changed allowed "
                "file. Do not return a partial or line-number-only patch."
            )
        sections.extend(
            [
                "This is a retry against a clean sandbox baseline. Use the prior "
                "candidate and verification evidence to make the smallest fix.",
                "Do not report BLOCKED merely because the previous candidate was "
                "malformed or failed to apply. Re-read the supplied clean context "
                "and return a valid replacement candidate; report BLOCKED only when "
                "the task itself is ambiguous, unsafe, or lacks required authority.",
                retry_output_rule,
                "Previous candidate (patch or files object):\n"
                + _truncate_retry_text(retry.previous_patch, 12000),
                "Failure summary:\n"
                + _truncate_retry_text(retry.failure_summary, 2500),
                "Verification evidence:\n"
                + json.dumps(
                    _bounded_retry_verification(retry.verification),
                    ensure_ascii=False,
                    indent=2,
                ),
            ]
        )
    return "\n\n".join(sections)


def _json_events(text: str) -> list[Mapping[str, Any]]:
    events: list[Mapping[str, Any]] = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            events.append(value)
    return events


def _last_agent_message(text: str) -> str:
    chunks: list[str] = []
    for event in _json_events(text):
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, Mapping) or item.get("type") != "agent_message":
            continue
        value = item.get("text")
        if isinstance(value, str):
            chunks.append(value)
    return "".join(chunks).strip()


def _normalize_reported_file_content(value: str) -> str:
    """Repair a common JSON fallback artifact without rewriting normal code."""

    if "\\n" not in value or "\n" in value:
        return value
    if not any(marker in value for marker in ("def ", "import ", "class ", "return ")):
        return value
    return value.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")


def _normalize_reported_file_value(value: Any) -> Any:
    if isinstance(value, str):
        return _normalize_reported_file_content(value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        content = "\n".join(value)
        return content + "\n" if content and not content.endswith("\n") else content
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], Mapping):
        # Some bounded workers wrap a ranged snippet as
        # ``[{"range": [start, end], "content": "..."}]``. The range is
        # advisory transport metadata; the task's declared context and the
        # outer ranged patch builder remain the authority for placement.
        return _normalize_reported_file_value(value[0])
    if isinstance(value, Mapping):
        for key in ("content", "text", "value", "lines"):
            if key in value:
                return _normalize_reported_file_value(value[key])
    return value


def _normalize_reported_files(value: Any) -> dict[str, Any]:
    """Normalize path maps and common path/content list envelopes."""

    if isinstance(value, Mapping):
        entries = list(value.items())
    elif isinstance(value, list):
        entries = []
        for item in value:
            if not isinstance(item, Mapping):
                raise ValueError("reported file entries must be path/content objects")
            path = next(
                (
                    item.get(key)
                    for key in ("path", "file", "name")
                    if item.get(key) is not None
                ),
                None,
            )
            content = next(
                (
                    item.get(key)
                    for key in ("content", "text", "value", "lines")
                    if key in item
                ),
                None,
            )
            if not isinstance(path, str) or content is None:
                raise ValueError(
                    "reported file entries require string path and content"
                )
            entries.append((path, content))
    else:
        raise ValueError("reported files must be a path map or entry list")

    normalized: dict[str, Any] = {}
    for path, content in entries:
        if not isinstance(path, str):
            raise ValueError("reported file paths must be strings")
        normalized[path] = _normalize_reported_file_value(content)
    return normalized


def _normalize_allowed_file_aliases(
    file_contents: tuple[tuple[str, str], ...],
    allowed_files: tuple[str, ...],
) -> tuple[tuple[tuple[str, str], ...], dict[str, str]]:
    """Repair only an unambiguous basename/stem typo in a file candidate.

    Small local models occasionally omit ``.py`` or the directory prefix in a
    complete-file response. Mapping that candidate is safe only when exactly
    one allowed path has the same basename or stem. Traversal, path-bearing
    guesses, and ambiguous matches remain unchanged and fail the normal scope
    gate.
    """

    normalized_allowed = tuple(path.replace("\\", "/") for path in allowed_files)
    aliases: dict[str, str] = {}
    normalized: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path, content in file_contents:
        candidate = path.replace("\\", "/")
        target = candidate
        if candidate not in normalized_allowed and not candidate.startswith(".") and "/" not in candidate:
            matches = [
                allowed
                for allowed in normalized_allowed
                if Path(allowed).name == candidate
                or Path(allowed).stem == candidate
            ]
            if len(matches) == 1 and matches[0] not in seen:
                target = matches[0]
                aliases[path] = target
        normalized.append((target, content))
        seen.add(target)
    return tuple(normalized), aliases


def _usage(text: str) -> dict[str, Any]:
    for event in reversed(_json_events(text)):
        if event.get("type") == "turn.completed":
            value = event.get("usage")
            if isinstance(value, Mapping):
                return dict(value)
    return {}


def _output_tail(value: Any, limit: int = 2000) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value)[-limit:]


def _escape_json_control_chars(value: str) -> str:
    """Escape raw control characters that appear inside JSON strings."""

    repaired: list[str] = []
    in_string = False
    escaped = False
    for character in value:
        if in_string:
            if escaped:
                repaired.append(character)
                escaped = False
                continue
            if character == "\\":
                repaired.append(character)
                escaped = True
                continue
            if character == '"':
                repaired.append(character)
                in_string = False
                continue
            if ord(character) < 0x20:
                repaired.append({
                    "\b": "\\b",
                    "\f": "\\f",
                    "\n": "\\n",
                    "\r": "\\r",
                    "\t": "\\t",
                }.get(character, f"\\u{ord(character):04x}"))
                continue
        elif character == '"':
            in_string = True
        repaired.append(character)
    return "".join(repaired)


def _repair_json_transport_noise(value: str) -> str:
    """Repair a narrow malformed-key escape emitted by small local models."""

    repaired = value
    for key in ("status", "summary", "patch", "files", "file_contents", "blockers"):
        repaired = repaired.replace(f'"{key}\\":\\"', f'"{key}":"')
        repaired = repaired.replace(f'"{key}\\":', f'"{key}":')
        # A serialized file value can end with an escaped structural quote,
        # followed by the next result key. Repair that boundary only; quotes
        # inside the file content remain untouched.
        repaired = re.sub(
            rf'\\?"\}},\\?"{re.escape(key)}\\?":',
            f'"}},"{key}":',
            repaired,
        )
    # Some responses close the file string but omit the closing brace for the
    # files object before the sibling blockers key. Insert that brace only
    # when a files map is present and the boundary is not already closed.
    files_match = re.search(r'"(?:files|file_contents)"\s*:\s*\{', repaired)
    blocker_matches = list(re.finditer(r',\s*"blockers"\s*:', repaired))
    blockers_boundary = blocker_matches[-1].start() if blocker_matches else -1
    if (
        files_match is not None
        and blockers_boundary > files_match.end()
        and not repaired[:blockers_boundary].rstrip().endswith("}")
    ):
        repaired = (
            repaired[:blockers_boundary]
            + "}"
            + repaired[blockers_boundary:]
        )
    # A malformed result occasionally emits the empty blockers array without
    # the key/value separator. Repair only that exact optional field; the
    # result still has to satisfy the normal response and patch gates.
    repaired = re.sub(r'"blockers"\s*\[\]', '"blockers":[]', repaired)
    repaired = re.sub(r'"blockers\[\]', '"blockers":[]', repaired)
    # Likewise, remove one transport escape when the file string is the last
    # value and the response ends with the two object-closing braces. Accept an
    # extra trailing quote produced after the escaped file boundary as noise;
    # the quote in the normalized form is the file-string terminator.
    repaired = re.sub(r'\\?"\}\}"?$', '"}}', repaired)
    return repaired


def _recover_truncated_single_file_result(value: str) -> Mapping[str, Any] | None:
    """Recover one complete file string when only the JSON envelope was cut."""

    match = re.search(r'"(?:files|file_contents)"\s*:\s*\{\s*', value)
    if match is None:
        return None
    cursor = match.end()
    if cursor >= len(value) or value[cursor] != '"':
        return None

    def read_json_string(start: int) -> tuple[str, int] | None:
        if start >= len(value) or value[start] != '"':
            return None
        escaped = False
        for index in range(start + 1, len(value)):
            character = value[index]
            if escaped:
                escaped = False
                continue
            if character == "\\":
                escaped = True
                continue
            if character == '"':
                token = value[start : index + 1]
                try:
                    json.loads(token)
                except json.JSONDecodeError:
                    return None
                return token, index + 1
        return None

    path_token = read_json_string(cursor)
    if path_token is None:
        return None
    path_text, cursor = path_token
    while cursor < len(value) and value[cursor].isspace():
        cursor += 1
    if cursor >= len(value) or value[cursor] != ":":
        return None
    cursor += 1
    while cursor < len(value) and value[cursor].isspace():
        cursor += 1
    content_token = read_json_string(cursor)
    if content_token is None:
        return None
    content_text, cursor = content_token
    suffix = value[cursor:].strip()
    if suffix and not re.fullmatch(
        r'(?:\}\s*,?\s*(?:"blockers"\s*:\s*(?:\[\s*\]|"[^"]*"))?\s*"?|,\s*"blockers"\s*:\s*(?:\[\s*\]|"[^"]*"))',
        suffix,
    ):
        return None
    try:
        path = json.loads(path_text)
        content = json.loads(content_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(path, str) or not path.strip() or not isinstance(content, str):
        return None
    return {
        "status": "READY",
        "summary": "Recovered a complete single-file candidate from a truncated result envelope.",
        "patch": "",
        "files": {path: content},
        "blockers": [],
    }


def _read_tail(path: Path, limit: int = 1_000_000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return _output_tail(data, limit)


def _progress_diagnostics(
    *,
    stdout_path: Path | None,
    stderr_path: Path,
    tail_limit: int = 4_000,
) -> dict[str, Any]:
    """Return bounded evidence for a stalled Codex process.

    The watchdog runs before the normal result record exists, so a no-progress
    failure used to lose the only useful evidence: whether Codex emitted its
    initial events, whether the provider wrote an error, and how much output was
    produced. Keep the tails short enough to be safe in an attempt ledger and
    leave the complete artifacts to the temporary runtime directory when a
    caller is collecting it separately.
    """

    def size(path: Path | None) -> int:
        if path is None:
            return 0
        try:
            return path.stat().st_size
        except OSError:
            return 0

    diagnostics: dict[str, Any] = {
        "stdout_bytes": size(stdout_path),
        "stderr_bytes": size(stderr_path),
    }
    stdout_tail = _read_tail(stdout_path, tail_limit) if stdout_path is not None else ""
    stderr_tail = _read_tail(stderr_path, tail_limit)
    if stdout_tail:
        diagnostics["stdout_tail"] = stdout_tail
    if stderr_tail:
        diagnostics["stderr_tail"] = stderr_tail
    return diagnostics


def _extract_unified_patch(text: str) -> str:
    """Extract a plainly returned unified diff from a malformed final message."""

    candidate = text.strip()
    # A worker may narrate a failed candidate and then print the corrected
    # patch in the same response. The last complete diff is the useful one;
    # the caller still runs git apply --check and scope verification.
    marker = candidate.rfind("diff --git ")
    if marker >= 0:
        candidate = candidate[marker:]
    else:
        lines = candidate.splitlines()
        hunk_start = next(
            (index for index, line in enumerate(lines) if line.startswith("@@ ")),
            None,
        )
        header_start = next(
            (
                index
                for index, line in enumerate(lines)
                if line.startswith("--- ")
                and index + 1 < len(lines)
                and lines[index + 1].startswith("+++ ")
            ),
            None,
        )
        start = header_start if header_start is not None else hunk_start
        if start is None:
            return ""
        candidate = "\n".join(lines[start:])
    fence = candidate.find("\n```")
    if fence >= 0:
        candidate = candidate[:fence]
    if not (
        candidate.startswith("diff --git ")
        or candidate.startswith("--- ")
        or candidate.startswith("@@ ")
    ):
        return ""
    return candidate.strip() + "\n"


def _extract_code_block(text: str) -> str:
    """Extract the last likely source-code fence from a prose response."""

    blocks = _extract_code_blocks(text)
    return blocks[-1] if blocks else ""


def _extract_code_blocks(text: str) -> list[str]:
    """Extract likely source fences in response order."""

    matches = list(
        re.finditer(
            r"```(?P<language>[^\r\n`]*)\r?\n(?P<body>.*?)```",
            text,
            flags=re.DOTALL,
        )
    )
    if not matches:
        return []
    preferred = [
        match for match in matches
        if match.group("language").strip().lower() in {"", "py", "python"}
    ]
    return [match.group("body").strip("\r\n") for match in (preferred or matches)]


def _line_change_file_candidate(
    sandbox_path: Path,
    task: DelegationTask,
    body: str,
) -> str | None:
    """Convert a bounded ``{"file", "changes"}`` recovery envelope.

    This format is accepted only for one ranged file. Line numbers select a
    replacement candidate; they are not trusted as a patch. The resulting full
    file is later checked by ``ranged_full_file_diff`` and the outer verifier.
    """

    try:
        value = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, Mapping) or len(task.allowed_files) != 1:
        return None
    raw_path = value.get("file") or value.get("path")
    changes = value.get("changes")
    if not isinstance(raw_path, str) or not isinstance(changes, list):
        return None
    try:
        path = normalize_relative_path(raw_path)
    except ValueError:
        return None
    allowed = normalize_relative_path(task.allowed_files[0])
    if path != allowed:
        return None
    ranged_specs = []
    for spec in task.context:
        context_path, start, end = context_path_and_range(spec)
        if start is not None and normalize_relative_path(context_path) == path:
            ranged_specs.append((start, end if end is not None else start))
    if len(ranged_specs) != 1 or task.context_mode == "insert_after":
        return None
    start, end = ranged_specs[0]
    entries: dict[int, str] = {}
    for change in changes:
        if not isinstance(change, Mapping):
            return None
        line = change.get("line")
        content = change.get("content")
        if (
            isinstance(line, bool)
            or not isinstance(line, int)
            or line < start
            or not isinstance(content, str)
            or "\n" in content
            or "\r" in content
            or line in entries
        ):
            return None
        entries[line] = content
    if not entries:
        return None

    target = sandbox_path / Path(*path.split("/"))
    try:
        target.resolve(strict=False).relative_to(sandbox_path.resolve())
    except ValueError:
        return None
    if target.is_symlink() or not target.is_file():
        return None
    old_content = target.read_text(encoding="utf-8", errors="replace")
    old_lines = old_content.replace("\r\n", "\n").replace("\r", "\n").splitlines(
        keepends=True
    )
    if end > len(old_lines):
        return None
    maximum = max(entries)
    # Keep malformed/hallucinated line numbers bounded even before the AST and
    # changed-line checks run.
    if maximum > max(len(old_lines), end) + 128:
        return None
    desired: list[str] = []
    for line in range(start, maximum + 1):
        if line in entries:
            desired.append(entries[line] + "\n")
        elif line <= len(old_lines):
            desired.append(old_lines[line - 1].replace("\r\n", "\n").replace("\r", "\n"))
        else:
            # A missing line in an expanded candidate must be explicit; do not
            # invent blank source lines between model-provided changes.
            return None
    return "".join(old_lines[: start - 1] + desired + old_lines[end:])


def _append_only_requested(task: DelegationTask) -> bool:
    text = " ".join(
        (
            task.objective,
            *task.requirements,
            *task.constraints,
            *task.success_criteria,
        )
    ).lower()
    return "append" in text


def _python_candidate_preserves_top_level_definitions(
    sandbox_path: Path,
    relative_path: str,
    candidate: str,
) -> bool:
    """Reject a source fence that is only a statement/function-body fragment.

    A no-tools fallback for a one-file Python task is interpreted as complete
    file content.  ``git apply --check`` can accept a replacement containing a
    valid statement such as ``if value < 0: ...`` even when it silently removes
    the original function.  Preserve every existing top-level function/class
    name before allowing that fallback.  New files and data-only modules do not
    need this guard.
    """

    if not relative_path.lower().endswith(".py"):
        return True
    target = sandbox_path / Path(*relative_path.split("/"))
    if not target.is_file():
        return True
    try:
        original_tree = ast.parse(
            target.read_text(encoding="utf-8", errors="replace")
        )
        candidate_tree = ast.parse(candidate)
    except (OSError, SyntaxError, UnicodeError):
        return False
    original_names = {
        node.name
        for node in ast.iter_child_nodes(original_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    if not original_names:
        return True
    candidate_names = {
        node.name
        for node in ast.iter_child_nodes(candidate_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    return original_names.issubset(candidate_names)


def _ranged_source_candidate(
    sandbox_path: Path,
    task: DelegationTask,
    body: str,
) -> str | None:
    """Extract the declared target definition from a prose/code-block reply.

    Small models often return the target function plus nearby context, or put
    a malformed unified hunk header in front of an otherwise usable function.
    For a ranged task, recover only the function/class that occupies the
    declared range. The subsequent ranged diff builder still enforces the
    exact placement and top-level shape.
    """

    if task.context_mode == "insert_after" or len(task.allowed_files) != 1:
        return None
    target_path = normalize_relative_path(task.allowed_files[0])
    target_ranges: list[tuple[int, int]] = []
    for spec in task.context:
        path, start, end = context_path_and_range(spec)
        if (
            start is not None
            and end is not None
            and normalize_relative_path(path) == target_path
        ):
            target_ranges.append((start, end))
    if not target_ranges:
        return None
    target = sandbox_path / Path(*target_path.split("/"))
    try:
        original_text = target.read_text(encoding="utf-8", errors="replace")
        original_tree = ast.parse(original_text)
    except (OSError, SyntaxError, UnicodeError):
        return None

    target_keys: set[tuple[type[ast.AST], str | None]] = set()
    for node in ast.walk(original_tree):
        if not isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            continue
        node_start = getattr(node, "lineno", 0)
        node_end = getattr(node, "end_lineno", node_start)
        if any(start <= node_start <= end or node_start <= start <= node_end
               for start, end in target_ranges):
            target_keys.add((type(node), getattr(node, "name", None)))
    if not target_keys:
        return None

    normalized_lines: list[str] = []
    for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith(("diff --git ", "index ", "@@ ", "--- ", "+++ ")):
            continue
        if line.startswith("-"):
            # Deleted-side diff lines are not part of the desired candidate.
            continue
        if line.startswith(("+", " ")):
            normalized_lines.append(line[1:])
        else:
            normalized_lines.append(line)
    variants = [body, "\n".join(normalized_lines)]
    for variant in variants:
        starts = [0]
        starts.extend(
            index
            for index, line in enumerate(variant.splitlines())
            if re.match(r"^(?:async\s+)?(?:def|class)\s+", line)
        )
        for start_index in dict.fromkeys(starts):
            candidate = "\n".join(variant.splitlines()[start_index:]).strip()
            if not candidate:
                continue
            try:
                tree = ast.parse(candidate)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                key = (type(node), getattr(node, "name", None))
                if key not in target_keys:
                    continue
                segment = ast.get_source_segment(candidate, node)
                if isinstance(segment, str) and segment.strip():
                    return segment.strip("\n") + "\n"

    # A common small-model failure is a diff-shaped fence with a context line
    # followed by ``+replacement`` but no ``-old`` line. Recover only when the
    # added statement has one unambiguous same-indentation/context partner in
    # the declared target definition; the resulting AST-shaped snippet still
    # goes through the ranged patch builder and outer verification.
    added_lines: list[str] = []
    context_lines: list[str] = []
    for raw_line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw_line.startswith(("diff --git ", "index ", "@@ ", "--- ", "+++ ")):
            continue
        if raw_line.startswith("+") and raw_line[1:].strip():
            added_lines.append(raw_line[1:])
        elif raw_line.startswith(" ") and raw_line[1:].strip():
            context_lines.append(raw_line[1:])
    if added_lines:
        for original_node in ast.walk(original_tree):
            key = (type(original_node), getattr(original_node, "name", None))
            if key not in target_keys:
                continue
            segment = ast.get_source_segment(original_text, original_node)
            if not isinstance(segment, str) or not segment.strip():
                continue
            candidate_lines = segment.splitlines(keepends=True)
            changed = False
            for added_line in added_lines:
                replacement = added_line.rstrip("\n") + "\n"
                exact_matches = [
                    index
                    for index, old_line in enumerate(candidate_lines)
                    if old_line.rstrip("\n") in {
                        value.rstrip("\n") for value in context_lines
                    }
                ]
                if len(exact_matches) != 1:
                    added_indent = len(added_line) - len(added_line.lstrip())
                    added_word = re.match(r"\s*([A-Za-z_]\w*)", added_line)
                    word = added_word.group(1) if added_word else None
                    fallback_matches = [
                        index
                        for index, old_line in enumerate(candidate_lines)
                        if len(old_line) - len(old_line.lstrip()) == added_indent
                        and (
                            word is None
                            or re.match(r"\s*([A-Za-z_]\w*)", old_line)
                            and re.match(r"\s*([A-Za-z_]\w*)", old_line).group(1) == word
                        )
                    ]
                    if len(fallback_matches) != 1:
                        break
                    exact_matches = fallback_matches
                candidate_lines[exact_matches[0]] = replacement
                changed = True
            if not changed:
                continue
            candidate = "".join(candidate_lines)
            try:
                tree = ast.parse(candidate)
            except SyntaxError:
                continue
            if any(
                (type(node), getattr(node, "name", None)) in target_keys
                for node in ast.walk(tree)
            ):
                return candidate.strip("\n") + "\n"
    return None


def _preserve_trailing_blank_lines(
    sandbox_path: Path,
    relative_path: str,
    body: str,
) -> str:
    """Keep omitted EOF separators out of a complete-file recovery diff."""

    target = sandbox_path / Path(*relative_path.replace("\\", "/").split("/"))
    try:
        original_lines = target.read_text(
            encoding="utf-8",
            errors="replace",
        ).replace("\r\n", "\n").replace("\r", "\n").splitlines(keepends=True)
    except OSError:
        return body
    candidate = body.replace("\r\n", "\n").replace("\r", "\n")
    if candidate and not candidate.endswith("\n"):
        candidate += "\n"
    original_trailing = 0
    for line in reversed(original_lines):
        if line.strip():
            break
        original_trailing += 1
    candidate_lines = candidate.splitlines(keepends=True)
    candidate_trailing = 0
    for line in reversed(candidate_lines):
        if line.strip():
            break
        candidate_trailing += 1
    if original_trailing > candidate_trailing:
        candidate += "\n" * (original_trailing - candidate_trailing)
    return candidate


def _recover_code_block_patch(
    sandbox_path: Path,
    task: DelegationTask,
    final_text: str,
) -> str:
    """Turn a one-file source fence into a checked bounded patch candidate."""

    if len(task.allowed_files) != 1:
        return ""
    bodies = _extract_code_blocks(final_text)
    if not bodies:
        return ""
    ranged_context = any(":" in spec for spec in task.context)
    candidates: list[tuple[tuple[int, int], str]] = []
    for body in bodies:
        try:
            body = _preserve_trailing_blank_lines(
                sandbox_path,
                task.allowed_files[0],
                body,
            )
            line_candidate = _line_change_file_candidate(sandbox_path, task, body)
            source_candidate = (
                _ranged_source_candidate(sandbox_path, task, body)
                if ranged_context
                else None
            )
            file_content = line_candidate or source_candidate or body
            if body.lstrip().startswith(("diff --git ", "--- ", "@@ ")):
                if _append_only_requested(task) and body.lstrip().startswith("@@ "):
                    candidate = append_hunk_diff(
                        sandbox_path,
                        task.allowed_files[0],
                        body,
                        task.allowed_files,
                    )
                else:
                    candidate = _extract_unified_patch(body)
                try:
                    check_patch(sandbox_path, candidate)
                except PatchError:
                    if source_candidate is None:
                        raise
                    candidate = ranged_replacement_diff(
                        sandbox_path,
                        {task.allowed_files[0]: source_candidate},
                        task.allowed_files,
                        task.context,
                        context_mode=task.context_mode,
                    )
            elif ranged_context:
                # Prefer a complete-file interpretation when the fence really
                # contains module setup or several declared definitions. Fall
                # back to the compact target/snippet interpretation for a
                # replacement or insert_after response.
                file_candidates: list[str] = [body]
                for value in (line_candidate, source_candidate, file_content):
                    if value and value not in file_candidates:
                        file_candidates.append(value)
                candidate = ""
                last_error: PatchError | None = None
                for value in file_candidates:
                    files = {task.allowed_files[0]: value}
                    try:
                        candidate = ranged_full_file_diff(
                            sandbox_path,
                            files,
                            task.allowed_files,
                            task.context,
                            context_mode=task.context_mode,
                        )
                    except PatchError as full_error:
                        last_error = full_error
                        try:
                            candidate = ranged_replacement_diff(
                                sandbox_path,
                                files,
                                task.allowed_files,
                                task.context,
                                context_mode=task.context_mode,
                            )
                        except PatchError as replacement_error:
                            last_error = replacement_error
                            continue
                    break
                if not candidate:
                    if last_error is not None:
                        raise last_error
                    raise PatchError("no bounded ranged recovery candidate")
            elif _append_only_requested(task):
                try:
                    candidate = append_diff(
                        sandbox_path,
                        {task.allowed_files[0]: body},
                        task.allowed_files,
                    )
                except PatchError:
                    candidate = replacement_diff(
                        sandbox_path,
                        {task.allowed_files[0]: body},
                        task.allowed_files,
                    )
            else:
                if not _python_candidate_preserves_top_level_definitions(
                    sandbox_path,
                    task.allowed_files[0],
                    body,
                ):
                    continue
                candidate = replacement_diff(
                    sandbox_path,
                    {task.allowed_files[0]: body},
                    task.allowed_files,
                )
            check_patch(sandbox_path, candidate)
            removed_lines = sum(
                line.startswith("-") and not line.startswith("---")
                for line in candidate.splitlines()
            )
            candidates.append(((removed_lines, len(candidate)), candidate))
        except PatchError:
            continue
    if not candidates:
        return ""
    return min(candidates, key=lambda item: item[0])[1]


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Terminate Codex and descendants before the worker sandbox is removed."""

    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _communicate_with_pull_guard(
    process: subprocess.Popen[bytes],
    *,
    prompt: str,
    timeout: float,
    stderr_path: Path,
    stdout_path: Path | None = None,
    idle_timeout: float | None = None,
    progress_probe: Callable[[], int] | None = None,
) -> bool:
    """Run Codex while aborting pulls and stalled no-progress lanes.

    ``Popen.communicate`` blocks until the child exits, but Codex reports an
    implicit Ollama pull on stderr before that can take minutes or hours.  A
    small monitor thread lets the parent inspect the file-backed stdout/stderr
    while preserving the existing artifacts and timeout behavior. The optional
    idle watchdog catches a model that starts a thread but never produces a
    response or tool-progress event.
    """

    outcome: dict[str, BaseException] = {}

    def communicate() -> None:
        try:
            process.communicate(
                input=prompt.encode("utf-8"),
                timeout=timeout,
            )
        except BaseException as exc:  # propagate subprocess and test doubles
            outcome["error"] = exc

    thread = threading.Thread(target=communicate, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    last_progress_at = time.monotonic()
    last_sizes: tuple[int, int] | None = None
    last_transport_progress: int | None = None
    transport_progress = 0
    pull_detected = False
    while thread.is_alive():
        stderr_text = _read_tail(stderr_path, 32_000)
        if "pulling model" in stderr_text.lower():
            pull_detected = True
            _terminate_process_tree(process)
            thread.join(timeout=10)
            break
        stdout_size = 0
        stderr_size = 0
        try:
            stdout_size = stdout_path.stat().st_size if stdout_path is not None else 0
        except OSError:
            pass
        try:
            stderr_size = stderr_path.stat().st_size
        except OSError:
            pass
        sizes = (stdout_size, stderr_size)
        if sizes != last_sizes:
            last_sizes = sizes
            last_progress_at = time.monotonic()
        if progress_probe is not None:
            try:
                current_transport_progress = max(0, int(progress_probe()))
            except (TypeError, ValueError, OSError):
                current_transport_progress = transport_progress
            if (
                last_transport_progress is None
                or current_transport_progress != last_transport_progress
            ):
                last_transport_progress = current_transport_progress
                transport_progress = current_transport_progress
                last_progress_at = time.monotonic()
        if (
            idle_timeout is not None
            and idle_timeout > 0
            and time.monotonic() - last_progress_at >= idle_timeout
        ):
            diagnostics = _progress_diagnostics(
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
            _terminate_process_tree(process)
            thread.join(timeout=10)
            raise CodexCliError(
                "Codex CLI made no stdout/stderr progress for "
                f"{idle_timeout:g} seconds",
                timed_out=True,
                runtime={
                    "idle_timeout_seconds": idle_timeout,
                    "no_progress_timeout": True,
                    "failure_kind": "codex_no_progress",
                    "recovery": (
                        "Do not start a long Codex run until the capability "
                        "smoke passes; warm the model or use a smaller task, "
                        "a different local model, or direct Ollama."
                    ),
                    "transport_progress_bytes": transport_progress,
                    **diagnostics,
                },
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_process_tree(process)
            thread.join(timeout=10)
            if thread.is_alive():
                raise subprocess.TimeoutExpired(
                    getattr(process, "args", "codex"), timeout
                )
            break
        thread.join(timeout=min(0.25, remaining))

    if thread.is_alive():
        # The child was killed above; do not leave a monitor holding the
        # artifact handles open while the sandbox is torn down.
        _terminate_process_tree(process)
        thread.join(timeout=10)
    error = outcome.get("error")
    if error is not None:
        raise error
    return pull_detected


class CodexCliWorker:
    """Run a local model through Codex CLI, returning only its sandbox diff."""

    def __init__(
        self,
        repo: str | Path,
        model: str | None = None,
        config: CodexCliConfig | None = None,
    ) -> None:
        self.repo = Path(repo).resolve()
        self.config = config or CodexCliConfig.from_env()
        self.model = model or self.config.default_model

    def _command(
        self,
        task: DelegationTask,
        sandbox_path: Path,
        final_message_path: Path,
        output_schema_path: Path | None = None,
    ) -> list[str]:
        model = task.model or self.model
        command = [
            self.config.executable,
            "exec",
            "-c",
            f"model_reasoning_effort={self.config.reasoning_effort}",
            "-c",
            "approval_policy=never",
            "-c",
            "shell_environment_policy.inherit=all",
            "--model",
            model,
            "--sandbox",
            self.config.sandbox,
            "--cd",
            str(sandbox_path),
            "--color",
            "never",
            "--json",
            "--output-last-message",
            str(final_message_path),
            "-",
        ]
        if output_schema_path is not None:
            command[-1:-1] = ["--output-schema", str(output_schema_path)]
        return command

    def _provider_config_text(
        self,
        *,
        model: str,
        provider_base_url: str,
    ) -> str:
        """Build the isolated Codex provider config for one worker attempt."""

        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.config.provider_id):
            raise CodexCliError(
                "AR_CODEX_PROVIDER_ID must contain only letters, digits, "
                "underscore, or hyphen"
            )
        if self.config.wire_api not in {"chat", "responses"}:
            raise CodexCliError(
                f"unsupported Codex provider wire API: {self.config.wire_api!r}"
            )
        return (
            "approval_policy = \"never\"\n"
            f"sandbox_mode = {json.dumps(self.config.sandbox)}\n"
            f"model = {json.dumps(model)}\n"
            f"model_provider = {json.dumps(self.config.provider_id)}\n\n"
            f"[model_providers.\"{self.config.provider_id}\"]\n"
            "name = \"Agent Relay Ollama\"\n"
            f"base_url = {json.dumps(provider_base_url.rstrip('/') + '/v1')}\n"
            f"wire_api = {json.dumps(self.config.wire_api)}\n\n"
            "[shell_environment_policy]\n"
            "inherit = \"all\"\n"
        )

    def _transport_runtime(
        self,
        proxy: OllamaCompatProxy | None,
    ) -> dict[str, Any]:
        rewrite_lane = proxy is not None and self.config.wire_api in {
            "chat",
            "responses",
        }
        rewrite_mode = (
            "disabled"
            if proxy is None
            else "chat_completions"
            if self.config.wire_api == "chat"
            else "responses"
        )
        return {
            "codex_provider_id": self.config.provider_id,
            "codex_wire_api": self.config.wire_api,
            "codex_sandbox_mode": self.config.sandbox,
            "compat_proxy_enabled": proxy is not None,
            "compat_proxy_target": self.config.ollama_host if proxy is not None else None,
            "reasoning_disabled": (
                self.config.disable_reasoning if rewrite_lane else False
            ),
            "codex_tools_stripped": (
                self.config.strip_tools if rewrite_lane else False
            ),
            "codex_prompt_compacted": (
                self.config.compact_prompt if rewrite_lane else False
            ),
            "compat_proxy_rewrite_mode": rewrite_mode,
            "ollama_num_ctx": self.config.ollama_num_ctx,
            "ollama_num_predict": self.config.ollama_num_predict,
            "ollama_temperature": self.config.ollama_temperature,
            "ollama_seed": self.config.ollama_seed,
            "compat_proxy_stats": proxy.stats if proxy is not None else {},
        }

    def _model_for_attempt(
        self,
        task: DelegationTask,
        retry: RetryEvidence | None,
    ) -> str:
        requested = task.model or self.model
        if retry is not None and self.config.retry_model:
            return self.config.retry_model
        return requested

    @staticmethod
    def _parse_final_result(text: str) -> WorkerResponse:
        """Apply the result schema locally for providers without JSON schema support."""

        candidate_text = text.strip()
        if candidate_text.startswith("```") and candidate_text.endswith("```"):
            candidate_text = "\n".join(candidate_text.splitlines()[1:-1]).strip()
        candidate_text = _escape_json_control_chars(candidate_text)
        candidate_text = _repair_json_transport_noise(candidate_text)
        try:
            value = json.loads(candidate_text)
        except json.JSONDecodeError as exc:
            decoder = json.JSONDecoder()
            candidates: list[Mapping[str, Any]] = []
            for index, character in enumerate(candidate_text):
                if character != "{":
                    continue
                try:
                    decoded, _ = decoder.raw_decode(candidate_text[index:])
                except json.JSONDecodeError:
                    continue
                if isinstance(decoded, Mapping):
                    candidates.append(decoded)
            value = next(
                (
                    item
                    for item in reversed(candidates)
                    if "status" in item
                    or any(key in item for key in ("patch", "files", "file_contents"))
                ),
                None,
            )
            if value is None:
                # Small local models occasionally emit a Python-dict-shaped
                # response (single quotes, True/False, or a trailing comma)
                # instead of JSON. literal_eval is non-executable and the
                # normal schema/path/scope/verification gates still apply.
                fragments = [candidate_text]
                first = candidate_text.find("{")
                last = candidate_text.rfind("}")
                if first >= 0 and last > first:
                    fragments.append(candidate_text[first : last + 1])
                for fragment in fragments:
                    try:
                        decoded = ast.literal_eval(fragment)
                    except (SyntaxError, ValueError, TypeError, MemoryError):
                        continue
                    if isinstance(decoded, Mapping):
                        value = decoded
                        break
            if value is None:
                value = _recover_truncated_single_file_result(candidate_text)
            if value is None:
                raise ValueError("final result was not valid JSON") from exc
            candidate_text = json.dumps(value, ensure_ascii=False)
        if not isinstance(value, Mapping):
            raise ValueError("final result must be a JSON object")
        if "status" not in value and not any(
            key in value for key in ("patch", "files", "file_contents")
        ):
            raise ValueError("final result must contain a status or candidate")
        normalized_result = dict(value)
        # Optional candidate fields are sometimes emitted as JSON null when
        # the model chose the other representation. Treat null as omitted so
        # the remaining patch/files candidate can still go through the normal
        # scope and apply gates.
        if normalized_result.get("patch") is None:
            normalized_result["patch"] = ""
        for key in ("files", "file_contents"):
            if normalized_result.get(key) is None:
                normalized_result.pop(key, None)
        normalized_result.setdefault("status", "READY")
        normalized_result.setdefault("summary", "")
        normalized_result.setdefault("blockers", [])
        value = normalized_result
        if not isinstance(value["status"], str):
            raise ValueError("final result status must be a string")
        if not isinstance(value["summary"], str):
            raise ValueError("final result summary must be a string")
        blockers = value["blockers"]
        if isinstance(blockers, list):
            if any(not isinstance(item, str) for item in blockers):
                raise ValueError("final result blockers must be an array of strings")
        elif not isinstance(blockers, str):
            raise ValueError("final result blockers must be an array or string")
        patch_value = value.get("patch", "")
        if isinstance(patch_value, Mapping):
            normalized = dict(value)
            normalized["patch"] = ""
            try:
                normalized["files"] = _normalize_reported_files(patch_value)
            except ValueError as exc:
                raise ValueError(
                    "final result patch objects must map paths to file content"
                ) from exc
            candidate_text = json.dumps(normalized, ensure_ascii=False)
        elif isinstance(patch_value, str):
            # Some local models serialize the path map one level too deeply:
            # {"patch": "{\"config.py\": \"...\"}"}.
            try:
                nested = json.loads(patch_value)
            except json.JSONDecodeError:
                nested = None
            if isinstance(nested, (Mapping, list)):
                try:
                    nested_files = _normalize_reported_files(nested)
                except ValueError:
                    pass
                else:
                    normalized = dict(value)
                    normalized["patch"] = ""
                    normalized["files"] = nested_files
                    candidate_text = json.dumps(normalized, ensure_ascii=False)
        else:
            raise ValueError("final result patch must be a string or path map")
        normalized = dict(json.loads(candidate_text))
        for key in ("files", "file_contents"):
            if normalized.get(key) is None:
                normalized.pop(key, None)
        # Codex-facing local models sometimes include a redundant list of
        # changed filenames beside a real unified patch. The list is not a
        # file-content candidate, so discard it only when the patch itself is
        # present; malformed file-only candidates remain rejected.
        if isinstance(normalized.get("patch"), str) and normalized["patch"].strip():
            for key in ("files", "file_contents"):
                candidate = normalized.get(key)
                if isinstance(candidate, list) and all(
                    isinstance(item, str) for item in candidate
                ):
                    normalized.pop(key)
                elif isinstance(candidate, str):
                    # A filename beside a real patch is metadata, not a
                    # file-content candidate. The patch itself remains subject
                    # to the normal path/scope/apply gates.
                    normalized.pop(key)
        for key in ("files", "file_contents"):
            if key in normalized:
                try:
                    normalized[key] = _normalize_reported_files(normalized[key])
                except ValueError as exc:
                    raise ValueError(
                        f"final result {key} must be a path map or entry list"
                    ) from exc
        candidate_text = json.dumps(normalized, ensure_ascii=False)
        return parse_worker_response(candidate_text, prefer_patch=True)

    def _preflight(
        self,
        *,
        model: str,
        environment: Mapping[str, str],
    ) -> dict[str, Any]:
        """Check the exact local runtime before creating a worker sandbox."""

        runtime: dict[str, Any] = {
            "model": model,
            "ollama_host": self.config.ollama_host,
            "model_pull_allowed": False,
            "output_schema_enabled": self.config.output_schema,
        }
        if self.config.require_model_present and self.config.local_provider in {
            "ollama",
            "ollama-chat",
        }:
            try:
                base_config = OllamaConfig.from_env()
                ollama_config = replace(
                    base_config,
                    host=self.config.ollama_host,
                    default_model=model,
                    timeout_seconds=min(self.config.timeout_seconds, 15.0),
                )
                models = OllamaClient(ollama_config).list_models()
            except (OllamaError, OSError, ValueError) as exc:
                raise CodexCliError(
                    f"Ollama preflight failed for {self.config.ollama_host}: {exc}"
                ) from exc
            model_names = sorted(
                {
                    str(item.get("name") or item.get("model"))
                    for item in models
                    if item.get("name") or item.get("model")
                }
            )
            if model not in model_names:
                raise CodexCliError(
                    f"Ollama model {model!r} is not installed at "
                    f"{self.config.ollama_host}; implicit model pulls are disabled. "
                    f"Installed models: {', '.join(model_names) or '<none>'}"
                )
            runtime["ollama_model_present"] = True
            runtime["ollama_installed_models"] = model_names

        if self.config.probe_version:
            try:
                version = subprocess.run(
                    [self.config.executable, "--version"],
                    cwd=self.repo,
                    env=dict(environment),
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=min(self.config.timeout_seconds, 15.0),
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise CodexCliError(
                    f"Codex CLI version probe failed for {self.config.executable}: {exc}"
                ) from exc
            version_text = (version.stdout or version.stderr).strip()
            if version.returncode != 0:
                raise CodexCliError(
                    f"Codex CLI version probe exited with code "
                    f"{version.returncode}: {version_text[-1000:]}"
                )
            runtime["codex_version"] = version_text or "<no version output>"
        else:
            runtime["codex_version"] = None
        return runtime

    def run(
        self,
        task: DelegationTask,
        context: str,
        retry: RetryEvidence | None = None,
    ) -> WorkerResponse:
        started = time.perf_counter()
        codex_home = Path(tempfile.mkdtemp(prefix="ar-codex-home-"))
        compat_proxy: OllamaCompatProxy | None = None
        try:
            requested_model = task.model or self.model
            model = self._model_for_attempt(task, retry)
            environment = os.environ.copy()
            environment["CODEX_HOME"] = str(codex_home)
            environment["OLLAMA_HOST"] = self.config.ollama_host
            preflight_runtime = self._preflight(
                model=model,
                environment=environment,
            )
            if self.config.compat_proxy_enabled:
                try:
                    compat_proxy = OllamaCompatProxy(
                        self.config.ollama_host,
                        disable_reasoning=self.config.disable_reasoning,
                        request_timeout=self.config.timeout_seconds,
                        num_ctx=self.config.ollama_num_ctx,
                        num_predict=self.config.ollama_num_predict,
                        temperature=self.config.ollama_temperature,
                        seed=self.config.ollama_seed,
                        strip_tools=self.config.strip_tools,
                        compact_prompt=self.config.compact_prompt,
                    ).start()
                except (OSError, ValueError) as exc:
                    raise CodexCliError(
                        f"could not start Ollama compatibility proxy: {exc}"
                    ) from exc
                provider_base_url = compat_proxy.base_url
            else:
                provider_base_url = self.config.ollama_host.rstrip("/")
                if provider_base_url.endswith("/v1"):
                    provider_base_url = provider_base_url[:-3].rstrip("/")
            (codex_home / "config.toml").write_text(
                self._provider_config_text(
                    model=model,
                    provider_base_url=provider_base_url,
                ),
                encoding="utf-8",
            )
            sandbox_context = GitSandbox(
                self.repo, f"codex-cli-{task.task_id}"
            )
            with sandbox_context as sandbox:
                if sandbox.path is None:
                    raise CodexCliError("Codex sandbox did not expose a path")
                final_message_path = sandbox.path / ".ar-codex-final-message.txt"
                output_schema_path: Path | None = None
                if self.config.output_schema:
                    output_schema_path = codex_home / "result.schema.json"
                    output_schema_path.write_text(
                        json.dumps(CODEX_RESULT_SCHEMA, ensure_ascii=False),
                        encoding="utf-8",
                    )
                command = self._command(
                    task,
                    sandbox.path,
                    final_message_path,
                    output_schema_path,
                )
                stdout_path = codex_home / "stdout.jsonl"
                stderr_path = codex_home / "stderr.log"
                process: subprocess.Popen[bytes] | None = None
                stdout_handle = stdout_path.open("wb")
                stderr_handle = stderr_path.open("wb")
                try:
                    process = subprocess.Popen(
                        command,
                        cwd=sandbox.path,
                        env=environment,
                        stdin=subprocess.PIPE,
                        stdout=stdout_handle,
                        stderr=stderr_handle,
                    )
                    model_pull_detected_early = _communicate_with_pull_guard(
                        process,
                        prompt=build_codex_prompt(task, context, retry),
                        timeout=self.config.timeout_seconds,
                        stderr_path=stderr_path,
                        stdout_path=stdout_path,
                        idle_timeout=self.config.idle_timeout_seconds,
                        progress_probe=(
                            lambda: compat_proxy.stats.get("bytes_out", 0)
                            if compat_proxy is not None
                            else 0
                        ),
                    )
                except FileNotFoundError as exc:
                    raise CodexCliError(
                        f"Codex CLI executable was not found: {self.config.executable}"
                    ) from exc
                except subprocess.TimeoutExpired as exc:
                    if process is not None:
                        _terminate_process_tree(process)
                        try:
                            process.communicate(timeout=10)
                        except subprocess.TimeoutExpired:
                            pass
                    stdout_tail = _read_tail(stdout_path)
                    stderr_tail = _read_tail(stderr_path)
                    diagnostics = _progress_diagnostics(
                        stdout_path=stdout_path,
                        stderr_path=stderr_path,
                    )
                    detail = " ".join(
                        item for item in (
                            f"stdout_tail={stdout_tail!r}" if stdout_tail else "",
                            f"stderr_tail={stderr_tail!r}" if stderr_tail else "",
                        )
                        if item
                    )
                    raise CodexCliError(
                        "Codex CLI timed out after "
                        f"{self.config.timeout_seconds:g} seconds"
                        + (f"; {detail}" if detail else ""),
                        timed_out=True,
                        runtime={
                            "failure_kind": "codex_timeout",
                            "timeout_seconds": self.config.timeout_seconds,
                            **diagnostics,
                        },
                    ) from exc
                finally:
                    stdout_handle.close()
                    stderr_handle.close()

                assert process is not None
                stdout = _read_tail(stdout_path)
                stderr = _read_tail(stderr_path)
                final_text = ""
                if final_message_path.is_file():
                    final_text = final_message_path.read_text(
                        encoding="utf-8", errors="replace"
                    ).strip()
                if not final_text:
                    final_text = _last_agent_message(stdout)
                final_message_path.unlink(missing_ok=True)
                sandbox.clean_verification_artifacts()
                inner_patch = capture_diff(sandbox.path)
                runtime: dict[str, Any] = {
                    **preflight_runtime,
                    "provider": "codex-cli",
                    "local_provider": self.config.local_provider,
                    "model": model,
                    "codex_exit_code": process.returncode,
                    "codex_wall_clock_seconds": time.perf_counter() - started,
                    "wall_clock_seconds": time.perf_counter() - started,
                    "stdout_bytes": stdout_path.stat().st_size
                    if stdout_path.exists()
                    else 0,
                    "stderr_bytes": stderr_path.stat().st_size
                    if stderr_path.exists()
                    else 0,
                    "final_message_chars": len(final_text),
                    "usage": _usage(stdout),
                    "inner_sandbox_mode": sandbox.mode,
                    "ollama_host": self.config.ollama_host,
                    "retry_model_used": (
                        retry is not None and model != requested_model
                    ),
                    **self._transport_runtime(compat_proxy),
                }
                model_pull_detected = (
                    model_pull_detected_early
                    or "pulling model" in stderr.lower()
                )
                runtime["model_pull_detected"] = model_pull_detected
                if stderr.strip():
                    runtime["stderr_tail"] = stderr[-2000:]
                if model_pull_detected:
                    raise CodexCliError(
                        "Codex CLI attempted an implicit Ollama model pull; "
                        "preinstall the exact model at the configured host",
                        runtime=runtime,
                    )
                if process.returncode != 0:
                    detail = (stderr or stdout).strip()
                    raise CodexCliError(
                        "Codex CLI exited with code "
                        f"{process.returncode}: {detail[-2000:]}",
                        runtime=runtime,
                    )

                try:
                    parsed = self._parse_final_result(final_text)
                except ValueError as exc:
                    runtime["final_message_preview"] = final_text[:4000]
                    extracted_patch = _extract_unified_patch(final_text)
                    code_block_patch = (
                        _recover_code_block_patch(sandbox.path, task, final_text)
                        if not inner_patch.strip() and not extracted_patch
                        else ""
                    )
                    fallback_patch = (
                        inner_patch.strip()
                        or extracted_patch
                        or code_block_patch
                    )
                    if fallback_patch:
                        runtime["final_result_fallback"] = (
                            "inner_sandbox_diff"
                            if inner_patch.strip()
                            else "reported_patch"
                            if extracted_patch
                            else "reported_code_block"
                        )
                        runtime["result_source"] = runtime["final_result_fallback"]
                        return WorkerResponse(
                            status="READY",
                            summary=(
                                "Codex returned a malformed structured result; "
                                "the bounded patch candidate was recovered."
                            ),
                            patch=fallback_patch,
                            runtime=runtime,
                            raw_response=final_text,
                        )
                    raise CodexCliError(
                        "Codex CLI returned a malformed structured result: "
                        f"{exc}",
                        retryable=True,
                        runtime=runtime,
                    ) from exc
                reported_patch = parsed.patch.strip()
                reported_patch_source = "reported_patch"
                normalized_files, file_aliases = _normalize_allowed_file_aliases(
                    parsed.file_contents,
                    task.allowed_files,
                )
                if file_aliases:
                    runtime["reported_file_path_aliases"] = file_aliases
                    parsed = replace(parsed, file_contents=normalized_files)
                reported_files = bool(parsed.file_contents)
                if reported_patch and not reported_files:
                    try:
                        check_patch(sandbox.path, reported_patch)
                    except PatchError as patch_error:
                        runtime["reported_patch_check_error"] = str(patch_error)[:1000]
                        recovered_patch = _recover_code_block_patch(
                            sandbox.path,
                            task,
                            final_text,
                        )
                        if recovered_patch:
                            reported_patch = recovered_patch
                            parsed = replace(parsed, patch=recovered_patch)
                            reported_patch_source = "reported_code_block"
                            runtime["final_result_fallback"] = "reported_code_block"
                            runtime["result_source"] = "reported_code_block"
                if inner_patch.strip() and (reported_patch or reported_files):
                    # The disposable sandbox is the stronger source of truth:
                    # it is the exact tool-produced diff and will be independently
                    # scope-checked and verified by the outer delegate. Discarding
                    # the duplicate candidate also keeps the handoff compact.
                    runtime["reported_candidate_ignored"] = (
                        "inner_sandbox_diff_authoritative"
                    )
                    reported_patch = ""
                    reported_files = False
                    parsed = replace(parsed, patch="", file_contents=())
                if parsed.blocked and (
                    inner_patch.strip() or reported_patch or reported_files
                ):
                    raise CodexCliError(
                        "Codex reported BLOCKED after modifying the sandbox"
                    )
                selected_source = "inner_sandbox_diff" if inner_patch.strip() else None
                if reported_patch and reported_files:
                    file_candidate_patch = ""
                    try:
                        file_contents = dict(parsed.file_contents)
                        file_candidate_patch = (
                            ranged_replacement_diff(
                                sandbox.path,
                                file_contents,
                                task.allowed_files,
                                task.context,
                                context_mode=task.context_mode,
                            )
                            if any(":" in spec for spec in task.context)
                            else replacement_diff(
                                sandbox.path,
                                file_contents,
                                task.allowed_files,
                            )
                        )
                    except PatchError as exc:
                        runtime["reported_files_candidate_error"] = str(exc)[:1000]

                    selected_patch = reported_patch
                    selected_source = "reported_patch"
                    if file_candidate_patch:
                        try:
                            check_patch(sandbox.path, reported_patch)
                        except PatchError as patch_error:
                            runtime["reported_patch_check_error"] = str(patch_error)[:1000]
                            try:
                                check_patch(sandbox.path, file_candidate_patch)
                            except PatchError as file_error:
                                runtime["reported_files_check_error"] = str(file_error)[:1000]
                            else:
                                selected_patch = file_candidate_patch
                                selected_source = "reported_files_fallback"
                    parsed = replace(
                        parsed,
                        patch=selected_patch,
                        file_contents=(),
                    )
                    reported_patch = selected_patch
                    reported_files = False
                patch = inner_patch or reported_patch
                if not parsed.blocked and not patch.strip() and not reported_files:
                    recovered_patch = _recover_code_block_patch(
                        sandbox.path,
                        task,
                        final_text,
                    )
                    if recovered_patch:
                        runtime["final_result_fallback"] = "reported_code_block"
                        runtime["result_source"] = "reported_code_block"
                        return replace(
                            parsed,
                            patch=recovered_patch,
                            file_contents=(),
                            runtime=runtime,
                            raw_response=final_text,
                        )
                    runtime["final_message_preview"] = final_text[:4000]
                    raise CodexCliError(
                        "Codex reported READY but produced no Git diff",
                        retryable=True,
                        runtime=runtime,
                    )
                runtime["result_source"] = (
                    selected_source
                    if selected_source is not None
                    else reported_patch_source
                    if reported_patch
                    else "reported_files"
                )
                return replace(
                    parsed,
                    patch=patch,
                    runtime=runtime,
                    raw_response=final_text,
                )
        except CodexCliError as exc:
            if compat_proxy is not None:
                exc.runtime.update(self._transport_runtime(compat_proxy))
            raise
        except SandboxError as exc:
            raise CodexCliError(f"could not create Codex sandbox: {exc}") from exc
        finally:
            if compat_proxy is not None:
                compat_proxy.stop()
            shutil.rmtree(codex_home, ignore_errors=True)
