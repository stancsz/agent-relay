from argparse import Namespace
import json
from pathlib import Path

from local_code_delegate import cli
from local_code_delegate.result import DelegationResult, ResultStatus


def test_eval_runner_loader_resolves_repo_local_evals() -> None:
    runner = cli._load_eval_runner(Path.cwd())

    assert runner.__name__ == "evals.runner"


def test_baseline_parser_exposes_explicit_codex_binary() -> None:
    args = cli._parser().parse_args(
        [
            "baseline",
            "--suite",
            "bounded-50",
            "--model",
            "gpt-5.6-luna",
            "--codex-bin",
            "C:/Codex/codex.exe",
            "--max-cases",
            "1",
        ]
    )

    assert args.command == "baseline"
    assert args.suite == "bounded-50"
    assert args.model == "gpt-5.6-luna"
    assert args.codex_bin == "C:/Codex/codex.exe"
    assert args.max_cases == 1


def _eval_args(tmp_path: Path) -> Namespace:
    return Namespace(
        backend="fixture",
        model=None,
        suite="bounded-basic",
        repo=tmp_path,
        output=None,
        economics=None,
        as_json=False,
    )


def test_eval_exit_requires_complete_mvp_gate(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "evals.runner.run_suite",
        lambda **_kwargs: {
            "status": "PASS",
            "mvp_gate": {"overall": "NOT_EVALUATED"},
        },
    )
    assert cli._eval(_eval_args(tmp_path)) == 2


def test_eval_exit_is_zero_for_complete_mvp_gate(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "evals.runner.run_suite",
        lambda **_kwargs: {
            "status": "PASS",
            "mvp_gate": {"overall": "PASS"},
        },
    )
    assert cli._eval(_eval_args(tmp_path)) == 0


def test_doctor_codex_smoke_reports_local_lane_failure(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    class FakeOllama:
        def __init__(self, _config) -> None:
            pass

        def list_models(self):
            return [{"name": "qwen3.5:4b", "digest": "digest"}]

    monkeypatch.setattr(cli, "OllamaClient", FakeOllama)
    monkeypatch.setattr(
        cli,
        "delegate_local",
        lambda **_kwargs: DelegationResult(
            task_id="codex-doctor-smoke",
            status=ResultStatus.TIMEOUT,
            summary="Codex local lane stalled.",
            metadata={
                "worker_runtime": {
                    "failure_kind": "codex_no_progress",
                    "stdout_bytes": 101,
                },
            },
        ),
    )
    args = Namespace(
        host="http://127.0.0.1:11435",
        model="qwen3.5:4b",
        smoke=False,
        codex_smoke=True,
        as_json=True,
    )

    assert cli._doctor(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["smoke"]["ok"] is None
    assert payload["codex_smoke"]["ok"] is False
    assert payload["codex_smoke"]["failure_kind"] == "codex_no_progress"


def test_delegate_patch_artifact_preserves_lf(monkeypatch, tmp_path: Path) -> None:
    task_path = tmp_path / "task.json"
    task_path.write_text(
        json.dumps({
            "task_id": "lf-patch",
            "objective": "Change the value.",
            "allowed_files": ["value.py"],
            "verification": ["python -c \"assert True\""],
        }),
        encoding="utf-8",
    )
    patch_path = tmp_path / "artifact.patch"
    fake_result = DelegationResult(
        task_id="lf-patch",
        status=ResultStatus.SUCCESS,
        patch="diff --git a/value.py b/value.py\n--- a/value.py\n+++ b/value.py\n",
    )
    monkeypatch.setattr(cli, "delegate_local", lambda **_kwargs: fake_result)
    args = Namespace(
        task=task_path,
        repo=tmp_path,
        backend="ollama",
        model=None,
        as_json=False,
        compact=False,
        patch_out=patch_path,
    )

    assert cli._delegate(args) == 0
    assert b"\r\n" not in patch_path.read_bytes()


def test_delegate_require_triage_fails_closed_before_worker(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    task_path = tmp_path / "task.json"
    task_path.write_text(
        json.dumps({
            "task_id": "needs-triage",
            "objective": "Change one value.",
            "allowed_files": ["value.py"],
            "verification": ["python -c \"assert True\""],
        }),
        encoding="utf-8",
    )
    called = False

    def unexpected_worker(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("worker must not run after triage rejection")

    monkeypatch.setattr(cli, "delegate_local", unexpected_worker)
    args = Namespace(
        task=task_path,
        repo=tmp_path,
        backend="ollama",
        model=None,
        as_json=False,
        compact=False,
        patch_out=None,
        require_triage=True,
        avoided_tokens=1800,
        spent_tokens=600,
        minimum_leverage=2.0,
    )

    assert cli._delegate(args) == 2
    assert called is False
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "TRIAGE_REJECTED"
    assert payload["triage"]["decision"] == "BLOCKED"


def test_delegate_require_triage_returns_decision_record_on_success(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    task_path = tmp_path / "task.json"
    task_path.write_text(
        json.dumps({
            "task_id": "safe-triage",
            "task_kind": "mechanical",
            "objective": "Change one value.",
            "allowed_files": ["value.py"],
            "verification": ["python -c \"assert True\""],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli,
        "delegate_local",
        lambda **_kwargs: DelegationResult(
            task_id="safe-triage",
            status=ResultStatus.SUCCESS,
            summary="verified",
        ),
    )
    args = Namespace(
        task=task_path,
        repo=tmp_path,
        backend="ollama",
        model=None,
        as_json=False,
        compact=True,
        patch_out=None,
        require_triage=True,
        avoided_tokens=1800,
        spent_tokens=600,
        minimum_leverage=2.0,
    )

    assert cli._delegate(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "SUCCESS"
    assert payload["triage"]["decision"] == "DELEGATE"
    assert payload["triage"]["economics"]["leverage"] == 3.0


def test_codex_backend_requires_triage_by_default(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    task_path = tmp_path / "task.json"
    task_path.write_text(
        json.dumps({
            "task_id": "codex-default-triage",
            "objective": "Change one value.",
            "allowed_files": ["value.py"],
            "verification": ["python -c \"assert True\""],
        }),
        encoding="utf-8",
    )
    called = False

    def unexpected_worker(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("untriaged Codex worker must not run")

    monkeypatch.setattr(cli, "delegate_local", unexpected_worker)
    args = Namespace(
        task=task_path,
        repo=tmp_path,
        backend="codex-ollama",
        model=None,
        as_json=False,
        compact=False,
        patch_out=None,
        require_triage=False,
        allow_untriaged=False,
        avoided_tokens=None,
        spent_tokens=None,
        minimum_leverage=2.0,
    )

    assert cli._delegate(args) == 2
    assert called is False
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "TRIAGE_REJECTED"
    assert payload["triage"]["decision"] == "BLOCKED"
