from pathlib import Path
import subprocess

import pytest

from evals.runner import _local_runtime
from agent_relay.codex_worker import CodexCliError
from agent_relay.delegate import _coerce_worker_patch, collect_context, delegate_local
from agent_relay.patch import PatchError, apply_patch, capture_diff
from agent_relay.patch import worktree_status
from agent_relay.result import ResultStatus, WorkerResponse
from agent_relay.sandbox import GitSandbox
from agent_relay.task import DelegationTask


def make_repo(path: Path) -> None:
    path.mkdir()
    (path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=path,
        check=True,
    )
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=path, check=True, capture_output=True)


def test_nested_repository_path_uses_copy_sandbox(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    fixture = repo / "fixtures" / "case"
    fixture.mkdir(parents=True)
    (fixture / "config.py").write_text("VALUE = 1\n", encoding="utf-8")

    with GitSandbox(fixture, "nested-fixture") as sandbox:
        assert sandbox.mode == "copy"
        assert sandbox.path is not None
        assert (sandbox.path / "config.py").read_text(encoding="utf-8") == "VALUE = 1\n"


class FixedWorker:
    def __init__(self, responses: list[WorkerResponse]) -> None:
        self.responses = responses
        self.calls = 0

    def run(self, task: DelegationTask, context: str, retry=None) -> WorkerResponse:
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


class InitialErrorWorker:
    def __init__(self, response: WorkerResponse) -> None:
        self.response = response
        self.calls = 0

    def run(self, task: DelegationTask, context: str, retry=None) -> WorkerResponse:
        self.calls += 1
        if self.calls == 1:
            raise ValueError("malformed model envelope")
        return self.response


def test_coerce_worker_patch_wraps_single_file_hunk(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    task = DelegationTask(
        task_id="single-file-hunk",
        objective="Change the value.",
        allowed_files=("value.py",),
        context=("value.py",),
        verification=("py -3 -c \"import value; assert value.VALUE == 2\"",),
    )
    response = WorkerResponse(
        status="READY",
        patch="@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n",
    )

    patch = _coerce_worker_patch(response, repo, task)
    apply_patch(repo, patch)

    assert (repo / "value.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_coerce_repairs_malformed_single_file_hunk_counts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    (repo / "math_utils.py").write_text(
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n"
        "\n\n"
        "def use_add(a: int, b: int) -> int:\n"
        "    return old_add(a, b)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add math fixture"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    task = DelegationTask(
        task_id="malformed-hunk",
        objective="replace the deprecated call",
        allowed_files=("math_utils.py",),
    )
    response = WorkerResponse(
        status="READY",
        patch=(
            "diff --git a/math_utils.py b/math_utils.py\n"
            "--- a/math_utils.py\n"
            "+++ b/math_utils.py\n"
            "@@ -5,2 +5,2 @@\n"
            "-    return old_add(a, b)\n"
            "+    return add(a, b)\n"
        ),
    )

    patch = _coerce_worker_patch(response, repo, task)
    apply_patch(repo, patch)

    assert "return add(a, b)" in (repo / "math_utils.py").read_text(encoding="utf-8")


def test_coerce_repairs_headerless_diff_with_malformed_plus_header(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    task = DelegationTask(
        task_id="malformed-file-headers",
        objective="change the value",
        allowed_files=("value.py",),
    )
    response = WorkerResponse(
        status="READY",
        patch=(
            "--- value.py\n"
            "+++value.py\n"
            "@@ -1 +1,3 @@\n"
            "+VALUE = 2\n"
            "+# verified\n"
            " VALUE = 1\n"
        ),
    )

    patch = _coerce_worker_patch(response, repo, task)
    apply_patch(repo, patch)

    assert (repo / "value.py").read_text(encoding="utf-8") == (
        "VALUE = 2\n# verified\nVALUE = 1\n"
    )


def test_coerce_appends_source_candidate_without_replacing_existing_tests(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    test_file = tests_dir / "test_value.py"
    test_file.write_text(
        "def test_existing() -> None:\n"
        "    assert True\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add test fixture"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    task = DelegationTask(
        task_id="append-test",
        objective="append one focused test",
        allowed_files=("tests/test_value.py",),
        constraints=("Append the new test after the existing test.",),
    )
    response = WorkerResponse(
        status="READY",
        patch=(
            "def test_new() -> None:\n"
            "    assert True\n"
        ),
    )

    patch = _coerce_worker_patch(response, repo, task)
    apply_patch(repo, patch)

    content = test_file.read_text(encoding="utf-8")
    assert "def test_existing" in content
    assert "def test_new" in content


def test_coerce_repairs_double_escaped_python_file_candidate(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    task = DelegationTask(
        task_id="escaped-file",
        objective="change the value",
        allowed_files=("value.py",),
    )
    response = WorkerResponse(
        status="READY",
        file_contents=(("value.py", r"VALUE = 2\n"),),
    )

    patch = _coerce_worker_patch(response, repo, task)
    apply_patch(repo, patch)

    assert (repo / "value.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_coerce_recovers_malformed_append_hunk(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    test_file = tests_dir / "test_value.py"
    test_file.write_text(
        "def test_existing() -> None:\n"
        "    assert True\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add append fixture"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    task = DelegationTask(
        task_id="append-hunk",
        objective="append one focused test",
        allowed_files=("tests/test_value.py",),
        constraints=("Append the new test after the existing test.",),
    )
    response = WorkerResponse(
        status="READY",
        patch=(
            "@@ -3,1 +3,5 @@\n"
            "    assert True\n"
            "def test_new() -> None:\n"
            "    assert True\n"
        ),
    )

    patch = _coerce_worker_patch(response, repo, task)
    apply_patch(repo, patch)

    assert "def test_existing" in test_file.read_text(encoding="utf-8")
    assert "def test_new" in test_file.read_text(encoding="utf-8")


def test_coerce_repairs_escaped_diff_line(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    task = DelegationTask(
        task_id="escaped-diff",
        objective="change the value",
        allowed_files=("value.py",),
    )
    response = WorkerResponse(
        status="READY",
        patch=(
            "diff --git a/value.py b/value.py\n"
            "--- a/value.py\n"
            "+++ b/value.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-VALUE = 1\n"
            "+VALUE = 2\\n\n"
        ),
    )

    patch = _coerce_worker_patch(response, repo, task)
    apply_patch(repo, patch)

    assert (repo / "value.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def patch_for(value: int) -> str:
    return (
        "diff --git a/value.py b/value.py\n"
        "--- a/value.py\n"
        "+++ b/value.py\n"
        "@@ -1 +1 @@\n"
        "-VALUE = 1\n"
        f"+VALUE = {value}\n"
    )


def test_delegate_success_uses_isolated_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    before = worktree_status(repo)
    worker = FixedWorker([
        WorkerResponse(status="READY", summary="set value", patch=patch_for(2)),
    ])
    task = DelegationTask(
        task_id="success",
        objective="set value",
        allowed_files=("value.py",),
        verification=("py -3 -c \"import value; assert value.VALUE == 2\"",),
    )
    result = delegate_local(task=task, repo=repo, worker=worker)
    assert result.status is ResultStatus.SUCCESS
    assert result.files_changed == ("value.py",)
    assert result.attempts == 1
    assert worktree_status(repo) == before
    assert worker.calls == 1


def test_delegate_retries_once_on_verification_failure(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    worker = FixedWorker([
        WorkerResponse(status="READY", summary="wrong", patch=patch_for(1)),
        WorkerResponse(status="READY", summary="fixed", patch=patch_for(2)),
    ])
    task = DelegationTask(
        task_id="retry",
        objective="set value",
        allowed_files=("value.py",),
        verification=("py -3 -c \"import value; assert value.VALUE == 2\"",),
        retry_limit=1,
    )
    result = delegate_local(task=task, repo=repo, worker=worker)
    assert result.status is ResultStatus.SUCCESS
    assert result.attempts == 2
    assert worker.calls == 2


def test_delegate_retries_once_on_patch_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    worker = FixedWorker([
        WorkerResponse(status="READY", summary="malformed", patch="not a diff"),
        WorkerResponse(status="READY", summary="fixed", patch=patch_for(2)),
    ])
    task = DelegationTask(
        task_id="patch-retry",
        objective="set value",
        allowed_files=("value.py",),
        verification=("py -3 -c \"import value; assert value.VALUE == 2\"",),
        retry_limit=1,
    )
    result = delegate_local(task=task, repo=repo, worker=worker)
    assert result.status is ResultStatus.SUCCESS
    assert result.attempts == 2
    assert worker.calls == 2


def test_delegate_retry_includes_rejected_file_candidate(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)

    class CandidateRetryWorker:
        calls = 0

        def run(self, task, context, retry=None):
            del task, context
            self.calls += 1
            if self.calls == 1:
                return WorkerResponse(
                    status="READY",
                    summary="returned a candidate that will fail verification",
                    file_contents=(("value.py", "VALUE = 1\n"),),
                )
            assert retry is not None
            assert '"files"' in retry.previous_patch
            assert "value.py" in retry.previous_patch
            return WorkerResponse(
                status="READY",
                summary="repaired candidate",
                patch=patch_for(2),
            )

    worker = CandidateRetryWorker()
    task = DelegationTask(
        task_id="file-candidate-retry",
        objective="set value",
        allowed_files=("value.py",),
        verification=("py -3 -c \"import value; assert value.VALUE == 2\"",),
        retry_limit=1,
    )

    result = delegate_local(task=task, repo=repo, worker=worker)

    assert result.status is ResultStatus.SUCCESS
    assert worker.calls == 2


def test_delegate_retries_once_on_initial_worker_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    worker = InitialErrorWorker(
        WorkerResponse(status="READY", summary="fixed", patch=patch_for(2))
    )
    task = DelegationTask(
        task_id="initial-retry",
        objective="set value",
        allowed_files=("value.py",),
        verification=("py -3 -c \"import value; assert value.VALUE == 2\"",),
        retry_limit=1,
    )
    result = delegate_local(task=task, repo=repo, worker=worker)
    assert result.status is ResultStatus.SUCCESS
    assert result.attempts == 2
    assert worker.calls == 2


def test_delegate_does_not_retry_codex_cli_infrastructure_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)

    class FailingCodexWorker:
        calls = 0

        def run(self, task, context, retry=None):
            self.calls += 1
            raise CodexCliError("local model is not installed")

    worker = FailingCodexWorker()
    task = DelegationTask(
        task_id="codex-preflight-failure",
        objective="set value",
        allowed_files=("value.py",),
        verification=("py -3 -c \"import value; assert value.VALUE == 2\"",),
        retry_limit=1,
    )

    result = delegate_local(task=task, repo=repo, worker=worker)

    assert result.status is ResultStatus.WORKER_ERROR
    assert result.attempts == 1
    assert worker.calls == 1
    assert result.metadata["main_worktree_unchanged"] is True


def test_delegate_retries_retryable_codex_result_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)

    class RetryableCodexWorker:
        calls = 0

        def run(self, task, context, retry=None):
            self.calls += 1
            if self.calls == 1:
                raise CodexCliError(
                    "malformed local result",
                    retryable=True,
                    runtime={
                        "provider": "codex-cli",
                        "model": "qwen3.5:4b",
                        "usage": {"input_tokens": 11, "output_tokens": 7},
                    },
                )
            return WorkerResponse(
                status="READY",
                summary="fixed",
                patch=patch_for(2),
                runtime={
                    "provider": "codex-cli",
                    "model": "qwen3.5:4b",
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                },
            )

    worker = RetryableCodexWorker()
    task = DelegationTask(
        task_id="codex-result-retry",
        objective="set value",
        allowed_files=("value.py",),
        verification=("py -3 -c \"import value; assert value.VALUE == 2\"",),
        retry_limit=1,
    )

    result = delegate_local(task=task, repo=repo, worker=worker)

    assert result.status is ResultStatus.SUCCESS
    assert result.attempts == 2
    assert worker.calls == 2
    assert [item["status"] for item in result.metadata["attempt_history"]] == [
        "WORKER_ERROR",
        "VERIFIED",
    ]
    assert [item["attempt"] for item in result.metadata["attempt_history"]] == [1, 2]
    runtime = _local_runtime(result)
    assert runtime["attempts"] == 2
    assert runtime["input_tokens"] == 16
    assert runtime["output_tokens"] == 10


def test_delegate_counts_failed_retry_attempt(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)

    class FailingRetryWorker:
        calls = 0

        def run(self, task, context, retry=None):
            self.calls += 1
            if self.calls == 1:
                return WorkerResponse(status="READY", summary="wrong", patch=patch_for(1))
            raise CodexCliError(
                "malformed retry result",
                retryable=True,
                runtime={"provider": "codex-cli"},
            )

    worker = FailingRetryWorker()
    task = DelegationTask(
        task_id="codex-retry-count",
        objective="set value",
        allowed_files=("value.py",),
        verification=("py -3 -c \"import value; assert value.VALUE == 2\"",),
        retry_limit=1,
    )

    result = delegate_local(task=task, repo=repo, worker=worker)

    assert result.status is ResultStatus.WORKER_ERROR
    assert result.attempts == 2
    assert result.metadata["attempt_history"][-1]["status"] == "WORKER_ERROR"


def test_collect_context_slices_literal_bracket_file_and_rejects_oob(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\n",
        encoding="utf-8",
    )
    task = DelegationTask(
        task_id="toml-context",
        objective="inspect project metadata",
        allowed_files=("pyproject.toml",),
        context=("pyproject.toml:1-1",),
    )
    assert "[project]" in collect_context(tmp_path, task)
    bad_task = DelegationTask(
        task_id="toml-context-oob",
        objective="inspect project metadata",
        allowed_files=("pyproject.toml",),
        context=("pyproject.toml:3-3",),
    )
    with pytest.raises(ValueError, match="outside file"):
        collect_context(tmp_path, bad_task)


def test_delegate_converts_complete_file_output_to_checked_diff(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    worker = FixedWorker([WorkerResponse(
        status="READY",
        summary="replaced file",
        file_contents=(("value.py", "VALUE = 3\n"),),
    )])
    task = DelegationTask(
        task_id="complete-file",
        objective="set value",
        allowed_files=("value.py",),
        verification=("py -3 -c \"import value; assert value.VALUE == 3\"",),
    )
    result = delegate_local(task=task, repo=repo, worker=worker)
    assert result.status is ResultStatus.SUCCESS
    assert result.files_changed == ("value.py",)
    assert "-VALUE = 1" in result.patch
    assert "+VALUE = 3" in result.patch


def test_delegate_converts_single_file_patch_content(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    worker = FixedWorker([WorkerResponse(
        status="READY",
        summary="replaced file",
        patch="VALUE = 4\n",
    )])
    task = DelegationTask(
        task_id="single-file-content",
        objective="set value",
        allowed_files=("value.py",),
        verification=("py -3 -c \"import value; assert value.VALUE == 4\"",),
    )
    result = delegate_local(task=task, repo=repo, worker=worker)
    assert result.status is ResultStatus.SUCCESS


def test_delegate_rejects_ready_patch_without_verification(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    worker = FixedWorker([WorkerResponse(
        status="READY",
        summary="set value",
        patch=patch_for(2),
    )])
    task = DelegationTask(
        task_id="missing-verification",
        objective="set value",
        allowed_files=("value.py",),
    )
    result = delegate_local(task=task, repo=repo, worker=worker)
    assert result.status is ResultStatus.WORKER_ERROR
    assert "verification" in result.blockers[0]
    assert worktree_status(repo) == ()


def test_delegate_rejects_complete_file_scope_violation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    worker = FixedWorker([WorkerResponse(
        status="READY",
        summary="bad scope",
        file_contents=(("other.py", "VALUE = 2\n"),),
    )])
    task = DelegationTask(
        task_id="complete-file-scope",
        objective="set value",
        allowed_files=("value.py",),
    )
    result = delegate_local(task=task, repo=repo, worker=worker)
    assert result.status is ResultStatus.SCOPE_VIOLATION
    assert worktree_status(repo) == ()


def test_delegate_accepts_ast_checked_target_range_snippet(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    (repo / "value.py").write_text(
        "def value() -> int:\n"
        "    return 1\n"
        "\n"
        "\n"
        "def other() -> int:\n"
        "    return 10\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "function baseline"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    worker = FixedWorker([WorkerResponse(
        status="READY",
        summary="updated target definition",
        file_contents=(("value.py", "def value() -> int:\n    return 2\n"),),
    )])
    task = DelegationTask(
        task_id="ranged-snippet",
        objective="change value",
        allowed_files=("value.py",),
        context=("value.py:1-2",),
        verification=("py -3 -c \"import value; assert value.value() == 2\"",),
    )
    result = delegate_local(task=task, repo=repo, worker=worker)
    assert result.status is ResultStatus.SUCCESS
    assert "-    return 1" in result.patch
    assert "+    return 2" in result.patch


def test_delegate_accepts_complete_file_when_changes_stay_in_target_range(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    (repo / "value.py").write_text(
        "def value() -> int:\n"
        "    return 1\n"
        "\n"
        "\n"
        "def other() -> int:\n"
        "    return 10\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "complete file baseline"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    worker = FixedWorker([WorkerResponse(
        status="READY",
        summary="updated target in complete file",
        file_contents=(
            (
                "value.py",
                "def value() -> int:\n"
                "    return 2\n"
                "\n"
                "\n"
                "def other() -> int:\n"
                "    return 10\n",
            ),
        ),
    )])
    task = DelegationTask(
        task_id="ranged-complete-file",
        objective="change value",
        allowed_files=("value.py",),
        context=("value.py:1-2",),
        verification=("py -3 -c \"import value; assert value.value() == 2\"",),
    )

    result = delegate_local(task=task, repo=repo, worker=worker)

    assert result.status is ResultStatus.SUCCESS
    assert "+    return 2" in result.patch
    assert "-    return 10" not in result.patch
    assert "+    return 10" not in result.patch


def test_coerce_accepts_complete_file_for_safe_insert_after_range(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    path = tests_dir / "test_helpers.py"
    path.write_text(
        "def test_existing():\n"
        "    assert True\n"
        "\n"
        "def test_other():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "insert after baseline"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    task = DelegationTask(
        task_id="insert-after-complete-file",
        objective="add one test",
        allowed_files=("tests/test_helpers.py",),
        context=("tests/test_helpers.py:1-2",),
        context_mode="insert_after",
    )
    full_file = (
        "def test_existing():\n"
        "    assert True\n"
        "\n"
        "def test_new_boundary():\n"
        "    assert True\n"
        "\n"
        "def test_other():\n"
        "    assert True\n"
    )

    patch = _coerce_worker_patch(
        WorkerResponse(
            status="READY",
            summary="added one test in complete file",
            file_contents=(("tests/test_helpers.py", full_file),),
        ),
        repo,
        task,
    )

    assert "test_new_boundary" in patch
    assert "-def test_existing" not in patch
    assert "+def test_existing" not in patch
    assert "-def test_other" not in patch
    assert "+def test_other" not in patch


def test_coerce_keeps_valid_insert_after_diff_at_declared_anchor(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    subprocess.run(
        ["git", "config", "core.autocrlf", "false"],
        cwd=repo,
        check=True,
    )
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    path = tests_dir / "test_helpers.py"
    path.write_bytes(
        (
            "def test_existing():\n"
            "    assert True\n"
            "\n"
            "def test_other():\n"
            "    assert True\n"
        ).encode("utf-8")
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "insert after diff baseline"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    task = DelegationTask(
        task_id="insert-after-diff",
        objective="append one test after the declared context",
        allowed_files=("tests/test_helpers.py",),
        context=("tests/test_helpers.py:1-2",),
        context_mode="insert_after",
    )
    path.write_bytes(
        (
            "def test_existing():\n"
            "    assert True\n"
            "\n"
            "def test_new_boundary():\n"
            "    assert True\n"
            "\n"
            "def test_other():\n"
            "    assert True\n"
        ).encode("utf-8")
    )
    response_patch = capture_diff(repo)
    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=repo, check=True, capture_output=True)
    patch = _coerce_worker_patch(
        WorkerResponse(
            status="READY",
            summary="added one test at the requested anchor",
            patch=response_patch,
        ),
        repo,
        task,
    )

    assert "@@ -1,5 +1,8 @@" in patch
    assert "@@ -4," not in patch


def test_delegate_accepts_target_snippet_with_read_only_range_context(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    task = DelegationTask(
        task_id="ranged-with-evidence",
        objective="set value",
        allowed_files=("value.py",),
        context=("value.py:1-1", "tests/test_value.py:1-3"),
    )
    patch = _coerce_worker_patch(
        WorkerResponse(
            status="READY",
            summary="updated target",
            file_contents=(("value.py", "VALUE = 2\n"),),
        ),
        repo,
        task,
    )
    assert "+VALUE = 2" in patch


def test_delegate_repairs_double_escaped_ranged_snippet_transport(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    (repo / "value.py").write_text(
        "def value() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    task = DelegationTask(
        task_id="ranged-escaped-snippet",
        objective="change value",
        allowed_files=("value.py",),
        context=("value.py:1-2",),
    )
    escaped = "def value() -> int:\\n    return 2\\n"
    patch = _coerce_worker_patch(
        WorkerResponse(
            status="READY",
            summary="updated target",
            file_contents=(("value.py", escaped),),
        ),
        repo,
        task,
    )
    assert "+    return 2" in patch


def test_delegate_rejects_scope_violation_before_sandbox_change(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    worker = FixedWorker([
        WorkerResponse(
            status="READY",
            summary="bad scope",
            patch=(
                "diff --git a/other.py b/other.py\n"
                "--- a/other.py\n"
                "+++ b/other.py\n"
                "@@ -0,0 +1 @@\n"
                "+VALUE = 2\n"
            ),
        ),
    ])
    task = DelegationTask(
        task_id="scope",
        objective="set value",
        allowed_files=("value.py",),
    )
    result = delegate_local(task=task, repo=repo, worker=worker)
    assert result.status is ResultStatus.SCOPE_VIOLATION
    assert worktree_status(repo) == ()


def test_ranged_context_requires_a_unified_diff(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    task = DelegationTask(
        task_id="ranged-output",
        objective="set value",
        allowed_files=("value.py",),
        context=("value.py:1-1",),
    )
    with pytest.raises(PatchError, match="ranged replacement"):
        _coerce_worker_patch(
            WorkerResponse(
                status="READY",
                summary="partial replacement",
                file_contents=(("value.py", "VALUE = 2\nOTHER = 3\n"),),
            ),
            repo,
            task,
        )
