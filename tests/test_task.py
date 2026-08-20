import pytest

from agent_relay.task import (
    DelegationTask,
    TaskContractError,
    context_path_and_range,
)


def test_task_normalizes_windows_separators_and_serializes() -> None:
    task = DelegationTask(
        task_id="t1",
        objective="bounded edit",
        allowed_files=(r"src\config.py",),
        context=("src/config.py:2-4",),
        verification=("pytest -q",),
    )
    assert task.allowed_files == ("src/config.py",)
    assert context_path_and_range(task.context[0]) == ("src/config.py", 2, 4)
    assert task.to_dict()["allowed_files"] == ["src/config.py"]


@pytest.mark.parametrize("path", ["../escape.py", r"C:\escape.py", r"C:relative.py", "/absolute.py", ""])
def test_task_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(TaskContractError):
        DelegationTask(
            task_id="t1",
            objective="bounded edit",
            allowed_files=(path,),
        )


def test_context_can_be_read_only_evidence_outside_write_scope() -> None:
    task = DelegationTask(
        task_id="t1",
        objective="bounded edit",
        allowed_files=("src/config.py",),
        context=("tests/test_config.py",),
    )
    assert task.context == ("tests/test_config.py",)


@pytest.mark.parametrize("spec", ["src/config.py:0-1", "src/config.py:2-1"])
def test_context_rejects_invalid_line_ranges(spec: str) -> None:
    with pytest.raises(TaskContractError):
        DelegationTask(
            task_id="t1",
            objective="bounded edit",
            allowed_files=("src/config.py",),
            context=(spec,),
        )


def test_retry_limit_is_bounded() -> None:
    with pytest.raises(TaskContractError, match="0 or 1"):
        DelegationTask(
            task_id="t1",
            objective="bounded edit",
            allowed_files=("config.py",),
            retry_limit=2,
        )


def test_context_mode_is_explicit_and_bounded() -> None:
    task = DelegationTask(
        task_id="t1",
        objective="add a test",
        allowed_files=("tests/test_config.py",),
        context=("tests/test_config.py:1-2",),
        context_mode="insert_after",
    )
    assert task.to_dict()["context_mode"] == "insert_after"
    with pytest.raises(TaskContractError, match="context_mode"):
        DelegationTask(
            task_id="t2",
            objective="bad mode",
            allowed_files=("config.py",),
            context_mode="append_anywhere",
        )
