from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time

import pytest

from local_code_delegate.codex_worker import (
    CodexCliConfig,
    CodexCliError,
    CodexCliWorker,
    _communicate_with_pull_guard,
    _extract_unified_patch,
    _normalize_allowed_file_aliases,
    _recover_code_block_patch,
    build_codex_prompt,
)
from local_code_delegate.task import DelegationTask
from local_code_delegate.worker import RetryEvidence


def _task(*, task_id: str = "codex-worker") -> DelegationTask:
    return DelegationTask(
        task_id=task_id,
        objective="Change VALUE from 1 to 2.",
        allowed_files=("value.py",),
        context=("value.py",),
        verification=("py -3 -c \"import value; assert value.VALUE == 2\"",),
    )


class FakeProcess:
    _next_pid = 41000

    def __init__(
        self,
        command: list[str],
        kwargs: dict[str, object],
        *,
        final_text: str = "",
        returncode: int = 0,
        stderr_text: str = "",
        edit_value: int | None = None,
        delay_after_stderr: float = 0.0,
    ) -> None:
        self.command = command
        self.stdout = kwargs["stdout"]
        self.stderr = kwargs["stderr"]
        self.cwd = Path(str(kwargs["cwd"]))
        self.final_text = final_text
        self.target_returncode = returncode
        self.stderr_text = stderr_text
        self.edit_value = edit_value
        self.delay_after_stderr = delay_after_stderr
        self.pid = FakeProcess._next_pid
        FakeProcess._next_pid += 1
        self.returncode: int | None = None

    def communicate(self, input=None, timeout=None):
        del input, timeout
        if self.edit_value is not None:
            (self.cwd / "value.py").write_text(
                f"VALUE = {self.edit_value}\n", encoding="utf-8"
            )
        output = b'{"type":"turn.completed","usage":{"input_tokens":12}}\n'
        self.stdout.write(output)
        self.stdout.flush()
        if self.stderr_text:
            self.stderr.write(self.stderr_text.encode("utf-8"))
            self.stderr.flush()
            if self.delay_after_stderr:
                deadline = time.perf_counter() + self.delay_after_stderr
                while self.returncode is None and time.perf_counter() < deadline:
                    time.sleep(0.01)
        if self.final_text:
            output_index = self.command.index("--output-last-message") + 1
            Path(self.command[output_index]).write_text(
                self.final_text, encoding="utf-8"
            )
        self.returncode = self.target_returncode
        return (None, None)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        del timeout
        if self.returncode is None:
            self.returncode = self.target_returncode
        return self.returncode

    def kill(self):
        self.returncode = -9


class StalledProcess:
    pid = 41999
    args = ["fake-codex"]

    def __init__(self) -> None:
        self.returncode: int | None = None

    def communicate(self, input=None, timeout=None):
        del input, timeout
        while self.returncode is None:
            time.sleep(0.01)
        return (None, None)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        del timeout
        self.returncode = self.returncode or -9
        return self.returncode

    def kill(self):
        self.returncode = -9


def _fake_config() -> CodexCliConfig:
    return CodexCliConfig(
        executable="fake-codex",
        require_model_present=False,
        probe_version=False,
        compat_proxy_enabled=False,
    )


def test_codex_cli_defaults_to_low_reasoning_for_bounded_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LCD_CODEX_REASONING_EFFORT", raising=False)
    config = CodexCliConfig.from_env()

    assert config.reasoning_effort == "low"
    assert config.timeout_seconds == 180.0
    assert config.idle_timeout_seconds == 90.0
    assert config.provider_id == "lcd-ollama"
    assert config.wire_api == "responses"
    assert config.sandbox == "danger-full-access"
    assert config.compat_proxy_enabled is True
    assert config.disable_reasoning is True
    assert config.strip_tools is True
    assert config.compact_prompt is True
    assert config.ollama_num_ctx == 8192
    assert config.ollama_num_predict is None
    assert config.ollama_temperature == 0.0
    assert config.ollama_seed is None


def test_codex_context_bound_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LCD_CODEX_NUM_CTX", "16384")

    config = CodexCliConfig.from_env()

    assert config.ollama_num_ctx == 16384


def test_build_codex_prompt_is_bounded_and_requires_verification() -> None:
    prompt = build_codex_prompt(_task(), "--- value.py ---\nVALUE = 1\n")

    assert "Allowed files (write scope):\n- value.py" in prompt
    assert "run every command before reporting READY" in prompt
    assert "Prefer using your tools to edit the sandbox" in prompt
    assert "keep the final JSON minimal" in prompt
    assert "make one focused edit pass" in prompt
    assert "Do not narrate a plan" in prompt
    assert '"status":"READY" or "BLOCKED"' in prompt
    assert "complete replacement content" in prompt


def test_retry_prompt_prefers_complete_file_recovery_for_one_file_task() -> None:
    prompt = build_codex_prompt(
        _task(),
        "--- value.py ---\nVALUE = 1\n",
        RetryEvidence(
            previous_patch="",
            verification=(),
            failure_summary="worker returned a malformed result",
        ),
    )

    assert "complete current content of the one allowed file" in prompt
    assert "line-number-only" in prompt


def test_file_candidate_alias_normalizes_only_unambiguous_stem() -> None:
    normalized, aliases = _normalize_allowed_file_aliases(
        (("config", "VALUE = 2\n"),),
        ("config.py",),
    )

    assert normalized == (("config.py", "VALUE = 2\n"),)
    assert aliases == {"config": "config.py"}


def test_file_candidate_alias_keeps_ambiguous_path_for_scope_rejection() -> None:
    normalized, aliases = _normalize_allowed_file_aliases(
        (("config", "VALUE = 2\n"),),
        ("src/config.py", "tests/config.py"),
    )

    assert normalized == (("config", "VALUE = 2\n"),)
    assert aliases == {}


def test_codex_worker_parses_jsonl_embedded_file_candidate() -> None:
    result = CodexCliWorker._parse_final_result(
        'agent message\n{"status":"READY","summary":"changed value",'
        '"patch":{"value.py":"VALUE = 2\\n"},"blockers":[]}\n'
    )

    assert result.status == "READY"
    assert result.summary == "changed value"
    assert result.file_contents == (("value.py", "VALUE = 2\n"),)


def test_codex_worker_parses_safe_python_dict_fallback() -> None:
    result = CodexCliWorker._parse_final_result(
        "Here is the result: "
        "{'status': 'READY', 'summary': 'changed value', "
        "'patch': '', 'files': {'value.py': 'VALUE = 2\\n'}, "
        "'blockers': [],}"
    )

    assert result.status == "READY"
    assert result.file_contents == (("value.py", "VALUE = 2\n"),)


def test_codex_worker_normalizes_escaped_python_file_lines() -> None:
    result = CodexCliWorker._parse_final_result(json.dumps({
        "status": "READY",
        "summary": "returned a serialized file",
        "files": {"value.py": r"def value():\n    return 2\n"},
        "blockers": [],
    }))

    assert result.file_contents == (("value.py", "def value():\n    return 2\n"),)


def test_codex_worker_repairs_escaped_json_object_keys() -> None:
    result = CodexCliWorker._parse_final_result(
        '{"status":"READY","summary\\":\\"changed value",'
        '"patch":"","files":{"value.py":"VALUE = 2\\n"},'
        '"blockers":[]}'
    )

    assert result.status == "READY"
    assert result.file_contents == (("value.py", "VALUE = 2\n"),)


def test_codex_worker_repairs_escaped_file_boundary_before_next_key() -> None:
    result = CodexCliWorker._parse_final_result(
        '{"status":"READY","summary":"changed value",'
        '"patch":"","files":{"value.py":"VALUE = 2\\n\\"},'
        '\\"blockers\\":[]}'
    )

    assert result.status == "READY"
    assert result.file_contents == (("value.py", "VALUE = 2\n"),)


def test_codex_worker_repairs_missing_files_object_brace() -> None:
    result = CodexCliWorker._parse_final_result(
        '{"status":"READY","summary":"changed value",'
        '"patch":"","files":{"value.py":"VALUE = 2\\n",'
        '"blockers":[]}'
    )

    assert result.file_contents == (("value.py", "VALUE = 2\n"),)


def test_codex_worker_repairs_escaped_final_file_boundary() -> None:
    result = CodexCliWorker._parse_final_result(
        '{"status":"READY","summary":"changed value",'
        '"patch":"","files":{"value.py":"VALUE = 2\\n\\"}}'
    )

    assert result.file_contents == (("value.py", "VALUE = 2\n"),)


def test_codex_worker_repairs_raw_newlines_in_file_json() -> None:
    result = CodexCliWorker._parse_final_result(
        '{"status":"READY","summary":"changed value",'
        '"patch":"","files":{"value.py":"VALUE = 2\n'
        'NEXT = 3\\n\\"}}"'
    )

    assert result.file_contents == (("value.py", "VALUE = 2\nNEXT = 3\n"),)


def test_codex_worker_repairs_missing_blockers_colon() -> None:
    result = CodexCliWorker._parse_final_result(
        '{"status":"READY","summary":"changed value",'
        '"patch":"","files":{"value.py":"VALUE = 2\\n"},'
        '"blockers[]}'
    )

    assert result.file_contents == (("value.py", "VALUE = 2\n"),)


def test_codex_worker_repairs_blockers_nested_in_files_map() -> None:
    result = CodexCliWorker._parse_final_result(
        '{"status":"READY","summary":"changed value",'
        '"patch":"","files":{"value.py":"VALUE = 2\\n", '
        '"blockers":[]}'
    )

    assert result.file_contents == (("value.py", "VALUE = 2\n"),)


def test_codex_worker_recovers_complete_file_from_truncated_envelope() -> None:
    result = CodexCliWorker._parse_final_result(
        '{"status":"READY","summary":"changed value",'
        '"patch":"","files":{"value.py":"VALUE = 2\\n"'
    )

    assert result.file_contents == (("value.py", "VALUE = 2\n"),)


def test_codex_worker_ignores_null_optional_candidate_fields() -> None:
    result = CodexCliWorker._parse_final_result(
        '{"status":"READY","summary":"changed value",'
        '"patch":"--- a/value.py\\n+++ b/value.py\\n@@ -1 +1 @@\\n-'
        'VALUE = 1\\n+VALUE = 2\\n","files":null,"blockers":[]}'
    )

    assert result.patch.startswith("--- a/value.py")
    assert result.file_contents == ()


def test_codex_worker_extracts_header_only_unified_diff() -> None:
    patch = _extract_unified_patch(
        "I made the change.\n\n"
        "--- a/value.py\n"
        "+++ b/value.py\n"
        "@@ -1 +1 @@\n"
        "-VALUE = 1\n"
        "+VALUE = 2\n"
    )

    assert patch.startswith("--- a/value.py\n+++ b/value.py\n")


def test_codex_worker_prefers_last_corrected_unified_diff() -> None:
    patch = _extract_unified_patch(
        "First attempt:\n"
        "```diff\n"
        "diff --git a/value.py b/value.py\n"
        "--- a/value.py\n+++ b/value.py\n@@ -1 +1 @@\n"
        "-VALUE = 1\n+VALUE = 0\n"
        "```\n"
        "Corrected attempt:\n"
        "```diff\n"
        "diff --git a/value.py b/value.py\n"
        "--- a/value.py\n+++ b/value.py\n@@ -1 +1 @@\n"
        "-VALUE = 1\n+VALUE = 2\n"
        "```"
    )

    assert "+VALUE = 2" in patch
    assert "+VALUE = 0" not in patch


def test_codex_worker_accepts_range_wrapped_file_content() -> None:
    result = CodexCliWorker._parse_final_result(
        json.dumps({
            "status": "READY",
            "summary": "changed target",
            "patch": "",
            "files": {
                "tasks.py": [{
                    "range": [4, 5],
                    "content": "def target(value: int) -> int:\n    return value + 1\n",
                }],
            },
            "blockers": [],
        })
    )

    assert result.file_contents == (
        ("tasks.py", "def target(value: int) -> int:\n    return value + 1\n"),
    )


def test_codex_worker_ignores_redundant_filename_list_with_patch() -> None:
    patch = (
        "diff --git a/value.py b/value.py\n"
        "--- a/value.py\n"
        "+++ b/value.py\n"
        "@@ -1 +1 @@\n"
        "-VALUE = 1\n"
        "+VALUE = 2\n"
    )

    result = CodexCliWorker._parse_final_result(json.dumps({
        "status": "READY",
        "summary": "changed value",
        "files": ["value.py"],
        "patch": patch,
        "blockers": [],
    }))

    assert result.patch == patch


def test_codex_worker_preserves_both_patch_candidates_for_sandbox_selection() -> None:
    patch = (
        "diff --git a/value.py b/value.py\n"
        "--- a/value.py\n"
        "+++ b/value.py\n"
        "@@ -1 +1 @@\n"
        "-VALUE = 1\n"
        "+VALUE = 2\n"
    )

    result = CodexCliWorker._parse_final_result(json.dumps({
        "status": "READY",
        "summary": "changed value",
        "patch": patch,
        "files": {"value.py": "VALUE = 1\n"},
        "blockers": [],
    }))

    assert result.patch == patch
    assert result.file_contents == (("value.py", "VALUE = 1\n"),)


def test_codex_worker_command_uses_native_output_schema(tmp_path: Path) -> None:
    schema = tmp_path / "result.schema.json"
    command = CodexCliWorker(
        repo=tmp_path,
        config=_fake_config(),
    )._command(
        _task(),
        tmp_path,
        tmp_path / "last-message.txt",
        schema,
    )

    schema_index = command.index("--output-schema")
    assert Path(command[schema_index + 1]) == schema


def test_codex_worker_uses_custom_provider_instead_of_legacy_oss_flags(
    tmp_path: Path,
) -> None:
    worker = CodexCliWorker(repo=tmp_path, config=_fake_config())
    command = worker._command(
        _task(),
        tmp_path,
        tmp_path / "last-message.txt",
    )

    assert "--oss" not in command
    assert "--local-provider" not in command
    assert command[command.index("--model") + 1] == "qwen3.5:4b"
    config_text = worker._provider_config_text(
        model="qwen3.5:4b",
        provider_base_url="http://127.0.0.1:34567",
    )
    assert 'model_provider = "lcd-ollama"' in config_text
    assert '[model_providers."lcd-ollama"]' in config_text
    assert 'base_url = "http://127.0.0.1:34567/v1"' in config_text
    assert 'wire_api = "responses"' in config_text
    assert "oss_provider" not in config_text


def test_codex_cli_config_defaults_to_qwen35_4b() -> None:
    assert CodexCliConfig().default_model == "qwen3.5:4b"


def test_codex_worker_can_pin_only_the_retry_to_the_target_model() -> None:
    config = CodexCliConfig(
        executable="fake-codex",
        require_model_present=False,
        probe_version=False,
        retry_model="qwen3.5:4b",
    )
    worker = CodexCliWorker(repo=Path.cwd(), model="qwen3.5:4b", config=config)
    retry = RetryEvidence(previous_patch="", verification=(), failure_summary="bad patch")

    assert worker._model_for_attempt(_task(), None) == "qwen3.5:4b"
    assert worker._model_for_attempt(_task(), retry) == "qwen3.5:4b"


def test_codex_worker_normalizes_file_line_lists() -> None:
    result = CodexCliWorker._parse_final_result(json.dumps({
        "status": "READY",
        "summary": "returned file lines",
        "files": {"value.py": ["VALUE = 2"]},
        "blockers": [],
    }))

    assert result.file_contents == (("value.py", "VALUE = 2\n"),)


def test_codex_worker_normalizes_path_content_file_entry_lists() -> None:
    result = CodexCliWorker._parse_final_result(json.dumps({
        "status": "READY",
        "summary": "returned file entries",
        "files": [{"path": "value.py", "content": ["VALUE = 2"]}],
        "blockers": [],
    }))

    assert result.file_contents == (("value.py", "VALUE = 2\n"),)


def test_codex_worker_captures_inner_diff_and_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    real_popen = subprocess.Popen

    def fake_popen(command: list[str], **kwargs):
        if not command or command[0] != "fake-codex":
            return real_popen(command, **kwargs)
        return FakeProcess(
            command,
            kwargs,
            final_text='{"status":"READY","summary":"changed value","blockers":[]}',
            edit_value=2,
        )

    monkeypatch.setattr("local_code_delegate.codex_worker.subprocess.Popen", fake_popen)
    result = CodexCliWorker(
        repo=tmp_path,
        model="qwen3.5:4b",
        config=_fake_config(),
    ).run(_task(), "--- value.py ---\nVALUE = 1\n")

    assert result.status == "READY"
    assert "value.py" in result.patch
    assert result.summary == "changed value"
    assert result.runtime["provider"] == "codex-cli"
    assert result.runtime["usage"]["input_tokens"] == 12
    assert "VALUE = 1" in (tmp_path / "value.py").read_text(encoding="utf-8")


def test_codex_worker_records_and_cleans_up_compat_proxy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    real_popen = subprocess.Popen
    proxy_state = {"started": 0, "stopped": 0}

    class FakeProxy:
        target_host = "http://127.0.0.1:11435"

        def __init__(self, *_args, **_kwargs) -> None:
            self.base_url = "http://127.0.0.1:45678"

        def start(self):
            proxy_state["started"] += 1
            return self

        def stop(self) -> None:
            proxy_state["stopped"] += 1

        @property
        def stats(self) -> dict[str, int]:
            return {
                "requests": 1,
                "rewritten_chat_requests": 1,
                "upstream_errors": 0,
                "bytes_in": 12,
                "bytes_out": 20,
            }

    def fake_popen(command: list[str], **kwargs):
        if not command or command[0] != "fake-codex":
            return real_popen(command, **kwargs)
        return FakeProcess(
            command,
            kwargs,
            final_text='{"status":"READY","summary":"changed value","blockers":[]}',
            edit_value=2,
        )

    monkeypatch.setattr("local_code_delegate.codex_worker.OllamaCompatProxy", FakeProxy)
    monkeypatch.setattr("local_code_delegate.codex_worker.subprocess.Popen", fake_popen)
    config = CodexCliConfig(
        executable="fake-codex",
        ollama_host="http://127.0.0.1:11435",
        require_model_present=False,
        probe_version=False,
        compat_proxy_enabled=True,
        wire_api="chat",
    )
    result = CodexCliWorker(repo=tmp_path, model="qwen3.5:4b", config=config).run(
        _task(task_id="proxy-runtime"),
        "--- value.py ---\nVALUE = 1\n",
    )

    assert result.runtime["codex_provider_id"] == "lcd-ollama"
    assert result.runtime["codex_wire_api"] == "chat"
    assert result.runtime["compat_proxy_enabled"] is True
    assert result.runtime["compat_proxy_target"] == "http://127.0.0.1:11435"
    assert result.runtime["reasoning_disabled"] is True
    assert result.runtime["compat_proxy_stats"]["rewritten_chat_requests"] == 1
    assert proxy_state == {"started": 1, "stopped": 1}


def test_codex_worker_preserves_blocked_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    real_popen = subprocess.Popen

    def fake_popen(command: list[str], **kwargs):
        if not command or command[0] != "fake-codex":
            return real_popen(command, **kwargs)
        return FakeProcess(
            command,
            kwargs,
            final_text=(
                '{"status":"BLOCKED","summary":"missing authority",'
                '"blockers":["requires credentials"]}'
            ),
        )

    monkeypatch.setattr("local_code_delegate.codex_worker.subprocess.Popen", fake_popen)
    result = CodexCliWorker(
        repo=tmp_path,
        config=_fake_config(),
    ).run(_task(task_id="blocked"), "--- value.py ---\nVALUE = 1\n")

    assert result.blocked
    assert result.blockers == ("requires credentials",)
    assert result.patch == ""


def test_codex_worker_accepts_reported_patch_when_tools_are_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    real_popen = subprocess.Popen
    reported_patch = (
        "diff --git a/value.py b/value.py\n"
        "--- a/value.py\n"
        "+++ b/value.py\n"
        "@@ -1 +1 @@\n"
        "-VALUE = 1\n"
        "+VALUE = 2\n"
    )

    def fake_popen(command: list[str], **kwargs):
        if not command or command[0] != "fake-codex":
            return real_popen(command, **kwargs)
        return FakeProcess(
            command,
            kwargs,
            final_text=json.dumps({
                "status": "READY",
                "summary": "reported a checked patch",
                "patch": reported_patch,
                "blockers": [],
            }),
        )

    monkeypatch.setattr("local_code_delegate.codex_worker.subprocess.Popen", fake_popen)
    result = CodexCliWorker(
        repo=tmp_path,
        config=_fake_config(),
    ).run(_task(task_id="reported-patch"), "--- value.py ---\nVALUE = 1\n")

    assert result.status == "READY"
    assert result.summary == "reported a checked patch"
    assert result.patch == reported_patch.strip()


def test_codex_worker_prefers_inner_diff_over_redundant_reported_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    real_popen = subprocess.Popen
    reported_patch = (
        "diff --git a/value.py b/value.py\n"
        "--- a/value.py\n"
        "+++ b/value.py\n"
        "@@ -1 +1 @@\n"
        "-VALUE = 1\n"
        "+VALUE = 2\n"
    )

    def fake_popen(command: list[str], **kwargs):
        if not command or command[0] != "fake-codex":
            return real_popen(command, **kwargs)
        return FakeProcess(
            command,
            kwargs,
            final_text=json.dumps({
                "status": "READY",
                "summary": "edited the sandbox and echoed a candidate",
                "patch": reported_patch,
                "blockers": [],
            }),
            edit_value=2,
        )

    monkeypatch.setattr("local_code_delegate.codex_worker.subprocess.Popen", fake_popen)
    result = CodexCliWorker(
        repo=tmp_path,
        config=_fake_config(),
    ).run(_task(task_id="inner-diff-authoritative"), "--- value.py ---\nVALUE = 1\n")

    assert result.status == "READY"
    assert result.runtime["result_source"] == "inner_sandbox_diff"
    assert result.runtime["reported_candidate_ignored"] == (
        "inner_sandbox_diff_authoritative"
    )
    assert "+VALUE = 2" in result.patch


def test_codex_worker_falls_back_to_file_candidate_when_patch_does_not_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    real_popen = subprocess.Popen
    bad_patch = (
        "diff --git a/value.py b/value.py\n"
        "--- a/value.py\n"
        "+++ b/value.py\n"
        "@@ -20 +20 @@\n"
        "-VALUE = 99\n"
        "+VALUE = 2\n"
    )

    def fake_popen(command: list[str], **kwargs):
        if not command or command[0] != "fake-codex":
            return real_popen(command, **kwargs)
        return FakeProcess(
            command,
            kwargs,
            final_text=json.dumps({
                "status": "READY",
                "summary": "used the applying candidate",
                "patch": bad_patch,
                "files": {"value.py": "VALUE = 2\n"},
                "blockers": [],
            }),
        )

    monkeypatch.setattr("local_code_delegate.codex_worker.subprocess.Popen", fake_popen)
    result = CodexCliWorker(
        repo=tmp_path,
        config=_fake_config(),
    ).run(_task(task_id="candidate-fallback"), "--- value.py ---\nVALUE = 1\n")

    assert result.status == "READY"
    assert result.runtime["result_source"] == "reported_files_fallback"
    assert result.file_contents == ()
    assert "+VALUE = 2" in result.patch


def test_codex_worker_recovers_source_fence_when_reported_patch_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    real_popen = subprocess.Popen
    bad_patch = "not a diff"

    def fake_popen(command: list[str], **kwargs):
        if not command or command[0] != "fake-codex":
            return real_popen(command, **kwargs)
        return FakeProcess(
            command,
            kwargs,
            final_text=(
                json.dumps({
                    "status": "READY",
                    "summary": "returned a bad patch and a source fallback",
                    "patch": bad_patch,
                    "blockers": [],
                })
                + "\n```python\nVALUE = 2\n```"
            ),
        )

    monkeypatch.setattr("local_code_delegate.codex_worker.subprocess.Popen", fake_popen)
    result = CodexCliWorker(
        repo=tmp_path,
        model="qwen3.5:4b",
        config=_fake_config(),
    ).run(_task(task_id="patch-source-fallback"), "--- value.py ---\nVALUE = 1\n")

    assert result.status == "READY"
    assert result.runtime["result_source"] == "reported_code_block"
    assert "+VALUE = 2" in result.patch


def test_codex_worker_recovers_diff_fence_after_empty_ready_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "value.py"
    path.write_text(
        "def value() -> int:\n"
        "    return 1\n",
        encoding="utf-8",
    )
    real_popen = subprocess.Popen
    task = DelegationTask(
        task_id="empty-envelope-diff-fallback",
        objective="Change value to return 2.",
        allowed_files=("value.py",),
        context=("value.py:1-2",),
    )
    diff = (
        "diff --git a/value.py b/value.py\n"
        "--- a/value.py\n"
        "+++ b/value.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def value() -> int:\n"
        "-    return 1\n"
        "+    return 2\n"
    )

    def fake_popen(command: list[str], **kwargs):
        if not command or command[0] != "fake-codex":
            return real_popen(command, **kwargs)
        return FakeProcess(
            command,
            kwargs,
            final_text=(
                json.dumps({
                    "status": "READY",
                    "summary": "changed value",
                    "patch": "",
                    "files": {},
                    "blockers": [],
                })
                + f"\n```diff\n{diff}```"
            ),
        )

    monkeypatch.setattr("local_code_delegate.codex_worker.subprocess.Popen", fake_popen)
    result = CodexCliWorker(
        repo=tmp_path,
        model="qwen3.5:4b",
        config=_fake_config(),
    ).run(task, "--- value.py ---\ndef value() -> int:\n    return 1\n")

    assert result.status == "READY"
    assert result.runtime["result_source"] == "reported_code_block"
    assert "+    return 2" in result.patch


def test_codex_worker_recovers_inner_diff_from_malformed_final_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    real_popen = subprocess.Popen

    def fake_popen(command: list[str], **kwargs):
        if not command or command[0] != "fake-codex":
            return real_popen(command, **kwargs)
        return FakeProcess(
            command,
            kwargs,
            final_text="not valid JSON",
            edit_value=2,
        )

    monkeypatch.setattr("local_code_delegate.codex_worker.subprocess.Popen", fake_popen)
    result = CodexCliWorker(
        repo=tmp_path,
        config=_fake_config(),
    ).run(_task(task_id="malformed-inner-diff"), "--- value.py ---\nVALUE = 1\n")

    assert result.status == "READY"
    assert result.runtime["result_source"] == "inner_sandbox_diff"
    assert "VALUE = 2" in result.patch


def test_codex_worker_recovers_single_file_code_block_from_prose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    real_popen = subprocess.Popen

    def fake_popen(command: list[str], **kwargs):
        if not command or command[0] != "fake-codex":
            return real_popen(command, **kwargs)
        return FakeProcess(
            command,
            kwargs,
            final_text=(
                "The bounded change is complete.\n\n"
                "```python\nVALUE = 2\n```"
            ),
        )

    monkeypatch.setattr("local_code_delegate.codex_worker.subprocess.Popen", fake_popen)
    result = CodexCliWorker(
        repo=tmp_path,
        model="qwen3.5:4b",
        config=_fake_config(),
    ).run(_task(task_id="prose-code-block"), "--- value.py ---\nVALUE = 1\n")

    assert result.status == "READY"
    assert result.runtime["result_source"] == "reported_code_block"
    assert "+VALUE = 2" in result.patch


def test_codex_worker_selects_code_block_that_preserves_existing_source(
    tmp_path: Path,
) -> None:
    path = tmp_path / "math_utils.py"
    path.write_text(
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n"
        "\n\n"
        "def use_add(a: int, b: int) -> int:\n"
        "    return old_add(a, b)\n",
        encoding="utf-8",
    )
    task = DelegationTask(
        task_id="preserve-code-block",
        objective="replace the old_add call with add",
        allowed_files=("math_utils.py",),
    )
    patch = _recover_code_block_patch(
        tmp_path,
        task,
        """The complete file is:
```python
def add(a: int, b: int) -> int:
    return a + b


def use_add(a: int, b: int) -> int:
    return add(a, b)
```
The final function is:
```python
def use_add(a: int, b: int) -> int:
    return add(a, b)
```""",
    )

    assert "-def add" not in patch
    assert "+    return add(a, b)" in patch


def test_codex_worker_recovers_target_definition_from_ranged_code_block(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tasks.py"
    path.write_text(
        "def keep() -> int:\n"
        "    return 1\n\n"
        "def target(value: int) -> int:\n"
        "    return value\n\n"
        "def after() -> int:\n"
        "    return 3\n",
        encoding="utf-8",
    )
    task = DelegationTask(
        task_id="ranged-code-block",
        objective="change target",
        allowed_files=("tasks.py",),
        context=("tasks.py:4-5",),
    )
    patch = _recover_code_block_patch(
        tmp_path,
        task,
        "The target is:\n"
        "```python\n"
        "def target(value: int) -> int:\n"
        "    return value + 1\n"
        "\n"
        "def after() -> int:\n"
        "    return 3\n"
        "```",
    )

    assert "+    return value + 1" in patch
    assert "-    return value" in patch
    assert "+def after" not in patch
    assert "-def after" not in patch


def test_codex_worker_repairs_diff_fence_missing_deletion_marker(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tasks.py"
    path.write_text(
        "def keep() -> int:\n"
        "    return 1\n\n"
        "def rename_fields(record: dict[str, str]) -> dict[str, str]:\n"
        "    return {\"first\": record[\"first_name\"]}\n\n",
        encoding="utf-8",
    )
    task = DelegationTask(
        task_id="malformed-diff-recovery",
        objective="include the last name in rename_fields",
        allowed_files=("tasks.py",),
        context=("tasks.py:4-5",),
    )
    patch = _recover_code_block_patch(
        tmp_path,
        task,
        "```diff\n"
        "@@ -4,2 +4,2 @@ def rename_fields(record):\n"
        "     return {\"first\": record[\"first_name\"]}\n"
        "+    return {\"first\": record[\"first_name\"], \"last\": record[\"last_name\"]}\n"
        "```",
    )

    assert "-    return {\"first\": record[\"first_name\"]}" in patch
    assert "+    return {\"first\": record[\"first_name\"], \"last\": record[\"last_name\"]}" in patch


def test_codex_worker_recovers_ranged_module_setup_from_complete_source_fence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "processor.py"
    path.write_text(
        "def process(item: str) -> str:\n"
        "    return item.strip()\n\n",
        encoding="utf-8",
    )
    task = DelegationTask(
        task_id="ranged-module-setup",
        objective="add module logging and log from process",
        allowed_files=("processor.py",),
        context=("processor.py:1-2",),
    )
    patch = _recover_code_block_patch(
        tmp_path,
        task,
        "```python\n"
        "import logging\n\n"
        "logger = logging.getLogger(__name__)\n\n"
        "def process(item: str) -> str:\n"
        "    logger.info('processing item')\n"
        "    return item.strip()\n"
        "```",
    )

    assert "+import logging" in patch
    assert "+logger = logging.getLogger(__name__)" in patch
    assert "+    logger.info('processing item')" in patch


def test_codex_worker_recovers_multiple_ranged_definitions_from_one_fence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "serializers.py"
    path.write_text(
        "def serialize_user(user: dict[str, object]) -> dict[str, object]:\n"
        "    return {\"name\": user[\"name\"]}\n\n\n"
        "def serialize_admin(user: dict[str, object]) -> dict[str, object]:\n"
        "    return {\"name\": user[\"name\"]}\n\n",
        encoding="utf-8",
    )
    task = DelegationTask(
        task_id="ranged-multiple-definitions",
        objective="include active in both serializers",
        allowed_files=("serializers.py",),
        context=("serializers.py:1-2", "serializers.py:5-6"),
    )
    patch = _recover_code_block_patch(
        tmp_path,
        task,
        "```python\n"
        "def serialize_user(user: dict[str, object]) -> dict[str, object]:\n"
        "    return {\"name\": user[\"name\"], \"active\": user[\"active\"]}\n\n\n"
        "def serialize_admin(user: dict[str, object]) -> dict[str, object]:\n"
        "    return {\"name\": user[\"name\"], \"active\": user[\"active\"]}\n"
        "```",
    )

    assert patch.count('+    return {"name": user["name"], "active": user["active"]}') == 2


def test_codex_worker_recovers_multiple_insert_after_tests_from_one_fence(
    tmp_path: Path,
) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    path = tests_dir / "test_value.py"
    path.write_text(
        "def test_existing() -> None:\n"
        "    assert True\n\n",
        encoding="utf-8",
    )
    task = DelegationTask(
        task_id="insert-multiple-tests",
        objective="append two focused tests",
        allowed_files=("tests/test_value.py",),
        context=("tests/test_value.py:1-2",),
        context_mode="insert_after",
    )
    patch = _recover_code_block_patch(
        tmp_path,
        task,
        "```python\n"
        "def test_empty() -> None:\n"
        "    assert True\n\n\n"
        "def test_none() -> None:\n"
        "    assert True\n"
        "```",
    )

    assert "+def test_empty" in patch
    assert "+def test_none" in patch


def test_codex_worker_recovers_append_only_code_block(
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "test_value.py"
    test_file.write_text(
        "def test_existing() -> None:\n"
        "    assert True\n",
        encoding="utf-8",
    )
    task = DelegationTask(
        task_id="append-code-block",
        objective="append one focused test",
        allowed_files=("test_value.py",),
        constraints=("Append the new test after the existing test.",),
    )

    patch = _recover_code_block_patch(
        tmp_path,
        task,
        "```python\ndef test_new() -> None:\n    assert True\n```",
    )

    assert "-def test_existing" not in patch
    assert "+def test_new" in patch


def test_codex_worker_rejects_python_fragment_that_removes_definition(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.py"
    path.write_text(
        "def parse_timeout(value: int) -> int:\n"
        "    return int(value)\n",
        encoding="utf-8",
    )
    task = DelegationTask(
        task_id="fragment-recovery",
        objective="Reject negative timeout values.",
        allowed_files=("config.py",),
    )

    patch = _recover_code_block_patch(
        tmp_path,
        task,
        "```python\nif value < 0: raise ValueError\n```",
    )

    assert patch == ""


def test_codex_worker_makes_rejected_python_fragment_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "config.py").write_text(
        "def parse_timeout(value: int) -> int:\n"
        "    return int(value)\n",
        encoding="utf-8",
    )
    real_popen = subprocess.Popen

    def fake_popen(command: list[str], **kwargs):
        if not command or command[0] != "fake-codex":
            return real_popen(command, **kwargs)
        return FakeProcess(
            command,
            kwargs,
            final_text="```python\nif value < 0: raise ValueError\n```",
        )

    monkeypatch.setattr("local_code_delegate.codex_worker.subprocess.Popen", fake_popen)
    task = DelegationTask(
        task_id="fragment-retryable",
        objective="Reject negative timeout values.",
        allowed_files=("config.py",),
    )
    with pytest.raises(CodexCliError, match="malformed structured result") as error:
        CodexCliWorker(
            repo=tmp_path,
            config=_fake_config(),
        ).run(task, "--- config.py ---\ndef parse_timeout(value: int) -> int:\n    return int(value)\n")

    assert error.value.retryable is True


def test_codex_worker_recovers_bounded_line_change_json_block(tmp_path: Path) -> None:
    path = tmp_path / "tasks.py"
    path.write_text(
        "def parse_port(value: str) -> int:\n"
        "    return int(value)\n",
        encoding="utf-8",
    )
    task = DelegationTask(
        task_id="line-change-envelope",
        objective="validate the parsed value",
        allowed_files=("tasks.py",),
        context=("tasks.py:1-2",),
    )
    final_text = (
        "```json\n"
        "{\"file\":\"tasks.py\",\"changes\":["
        "{\"line\":2,\"content\":\"    return 2\"}]}\n"
        "```"
    )

    patch = _recover_code_block_patch(tmp_path, task, final_text)

    assert "+    return 2" in patch


def test_codex_worker_surfaces_cli_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    real_popen = subprocess.Popen

    def fake_popen(command: list[str], **kwargs):
        if not command or command[0] != "fake-codex":
            return real_popen(command, **kwargs)
        return FakeProcess(
            command,
            kwargs,
            returncode=7,
            stderr_text="local failure",
        )

    monkeypatch.setattr("local_code_delegate.codex_worker.subprocess.Popen", fake_popen)
    with pytest.raises(CodexCliError, match="code 7"):
        CodexCliWorker(
            repo=tmp_path,
            config=_fake_config(),
        ).run(_task(task_id="failure"), "--- value.py ---\nVALUE = 1\n")


def test_codex_worker_rejects_implicit_model_pull(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    real_popen = subprocess.Popen

    def fake_popen(command: list[str], **kwargs):
        if not command or command[0] != "fake-codex":
            return real_popen(command, **kwargs)
        return FakeProcess(
            command,
            kwargs,
            stderr_text="Pulling model qwen3.5:4b...\n",
        )

    monkeypatch.setattr("local_code_delegate.codex_worker.subprocess.Popen", fake_popen)
    with pytest.raises(CodexCliError, match="implicit Ollama model pull"):
        CodexCliWorker(
            repo=tmp_path,
            config=_fake_config(),
        ).run(_task(task_id="implicit-pull"), "--- value.py ---\nVALUE = 1\n")


def test_codex_worker_aborts_model_pull_before_process_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    real_popen = subprocess.Popen

    def fake_popen(command: list[str], **kwargs):
        if not command or command[0] != "fake-codex":
            return real_popen(command, **kwargs)
        return FakeProcess(
            command,
            kwargs,
            stderr_text="Pulling model qwen3:30b...\n",
            delay_after_stderr=2.0,
        )

    monkeypatch.setattr("local_code_delegate.codex_worker.subprocess.Popen", fake_popen)
    started = time.perf_counter()
    with pytest.raises(CodexCliError, match="implicit Ollama model pull") as caught:
        CodexCliWorker(
            repo=tmp_path,
            config=_fake_config(),
        ).run(_task(task_id="early-pull"), "--- value.py ---\nVALUE = 1\n")

    assert time.perf_counter() - started < 1.5
    assert caught.value.runtime["model_pull_detected"] is True


def test_codex_worker_aborts_stalled_no_progress_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = StalledProcess()
    (tmp_path / "stderr.log").write_text(
        "provider connected but no completion\n",
        encoding="utf-8",
    )
    (tmp_path / "stdout.jsonl").write_text(
        '{"type":"thread.started"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "local_code_delegate.codex_worker._terminate_process_tree",
        lambda value: value.kill(),
    )

    with pytest.raises(CodexCliError, match="no stdout/stderr progress") as caught:
        _communicate_with_pull_guard(
            process,
            prompt="bounded task",
            timeout=2.0,
            stderr_path=tmp_path / "stderr.log",
            stdout_path=tmp_path / "stdout.jsonl",
            idle_timeout=0.05,
        )

    assert caught.value.timed_out is True
    assert caught.value.runtime["no_progress_timeout"] is True
    assert caught.value.runtime["failure_kind"] == "codex_no_progress"
    assert caught.value.runtime["stdout_bytes"] > 0
    assert caught.value.runtime["stderr_bytes"] > 0
    assert "thread.started" in caught.value.runtime["stdout_tail"]
    assert "provider connected" in caught.value.runtime["stderr_tail"]


def test_codex_worker_no_progress_diagnostics_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = StalledProcess()
    (tmp_path / "stderr.log").write_text("e" * 20_000, encoding="utf-8")
    (tmp_path / "stdout.jsonl").write_text("o" * 20_000, encoding="utf-8")
    monkeypatch.setattr(
        "local_code_delegate.codex_worker._terminate_process_tree",
        lambda value: value.kill(),
    )

    with pytest.raises(CodexCliError) as caught:
        _communicate_with_pull_guard(
            process,
            prompt="bounded task",
            timeout=2.0,
            stderr_path=tmp_path / "stderr.log",
            stdout_path=tmp_path / "stdout.jsonl",
            idle_timeout=0.05,
        )

    assert len(caught.value.runtime["stdout_tail"]) <= 4_000
    assert len(caught.value.runtime["stderr_tail"]) <= 4_000
