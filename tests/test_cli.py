from argparse import Namespace
import json
from pathlib import Path

from agent_relay import cli
from agent_relay.codex_review import CodexReviewResult
from agent_relay.result import DelegationResult, ResultStatus


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


def test_delegate_parser_exposes_sandboxed_claude_lane() -> None:
    args = cli._parser().parse_args(
        ["delegate", "--backend", "claude-task", "--task", "task.json"]
    )

    assert args.backend == "claude-task"


def test_escalation_parser_exposes_plan_and_review_end_gates() -> None:
    parser = cli._parser()

    plan = parser.parse_args([
        "escalate", "--task", "task.json", "--stage", "plan_end",
        "--signals-json", '{"ambiguity":true}',
    ])
    consult = parser.parse_args([
        "consult", "--task", "task.json", "--stage", "review_end",
        "--model", "gpt-5.6-sol", "--reasoning-effort", "high",
    ])

    assert plan.command == "escalate"
    assert plan.stage == "plan_end"
    assert consult.command == "consult"
    assert consult.model == "gpt-5.6-sol"


def test_escalate_command_returns_machine_readable_second_opinion_decision(tmp_path, capsys) -> None:
    task_path = tmp_path / "task.json"
    task_path.write_text(json.dumps({
        "task_id": "escalation-cli",
        "objective": "Change one value.",
        "allowed_files": ["value.py"],
        "verification": ["python -c \"assert True\""],
        "task_kind": "mechanical",
    }), encoding="utf-8")
    args = Namespace(
        task=task_path,
        stage="plan_end",
        policy=None,
        signals_json=None,
        as_json=True,
    )

    assert cli._escalate(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ESCALATION_DECISION"
    assert payload["decision"]["action"] == "consult"
    assert payload["decision"]["profile"]["model"] == "gpt-5.6-sol"


def test_consult_command_does_not_invoke_codex_when_policy_continues(tmp_path, capsys) -> None:
    task_path = tmp_path / "task.json"
    task_path.write_text(json.dumps({
        "task_id": "no-consult",
        "objective": "Change one value.",
        "allowed_files": ["value.py"],
        "verification": ["python -c \"assert True\""],
    }), encoding="utf-8")
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps({
        "version": 1,
        "enabled": True,
        "default_action": "continue",
        "profiles": {},
        "rules": [],
    }), encoding="utf-8")
    args = Namespace(
        task=task_path,
        repo=tmp_path,
        stage="execute",
        policy=policy_path,
        signals_json=None,
        prompt=None,
        codex_bin=None,
        model=None,
        reasoning_effort=None,
        timeout_seconds=None,
        force=False,
        as_json=True,
    )

    assert cli._consult(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "NOT_REQUIRED"


def test_consult_command_invokes_selected_sol_profile_and_returns_receipt(
    tmp_path, monkeypatch, capsys
) -> None:
    task_path = tmp_path / "task.json"
    task_path.write_text(json.dumps({
        "task_id": "consult-sol",
        "objective": "Review the candidate plan.",
        "allowed_files": ["value.py"],
        "verification": ["python -c \"assert True\""],
    }), encoding="utf-8")
    calls = []

    def fake_review(repo, **kwargs):
        calls.append((repo, kwargs))
        return CodexReviewResult(
            status="PASS",
            summary="No actionable findings.",
            findings="No actionable findings.",
            return_code=0,
            duration_seconds=0.1,
            runtime={"model": kwargs["config"].model},
        )

    monkeypatch.setattr(cli, "run_codex_review", fake_review)
    args = Namespace(
        task=task_path,
        repo=tmp_path,
        stage="plan_end",
        policy=None,
        signals_json=None,
        prompt=None,
        codex_bin="codex-test",
        model=None,
        reasoning_effort=None,
        timeout_seconds=5,
        force=False,
        as_json=True,
    )

    assert cli._consult(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "CONSULTED"
    assert payload["decision"]["profile"]["model"] == "gpt-5.6-sol"
    assert calls[0][0] == tmp_path
    assert calls[0][1]["uncommitted"] is False
    assert calls[0][1]["config"].model == "gpt-5.6-sol"
    assert calls[0][1]["config"].reasoning_effort == "high"


def test_failed_sol_review_returns_bounded_revise_then_human_review(
    tmp_path, monkeypatch, capsys
) -> None:
    task_path = tmp_path / "task.json"
    task_path.write_text(json.dumps({
        "task_id": "consult-revise",
        "objective": "Review the candidate.",
        "allowed_files": ["value.py"],
        "verification": ["python -c \"assert True\""],
    }), encoding="utf-8")
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps({
        "version": 1,
        "enabled": True,
        "default_action": "continue",
        "profiles": {"v": {"lane": "codex-review", "model": "sol", "role": "verifier"}},
        "rules": [{
            "id": "review",
            "priority": 1,
            "stages": ["review_end"],
            "action": "require_review",
            "profile": "v",
            "max_revisions": 1,
            "on_reject": "revise",
            "on_exhausted": "human_review",
        }],
    }), encoding="utf-8")
    monkeypatch.setattr(cli, "run_codex_review", lambda *_args, **_kwargs: CodexReviewResult(
        status="FAILED",
        summary="findings",
        findings="Defect found",
        return_code=1,
        duration_seconds=0.1,
        runtime={},
    ))

    base = dict(
        task=task_path, repo=tmp_path, stage="review_end", policy=policy_path,
        signals_json=None, prompt=None, codex_bin="codex-test", model=None,
        reasoning_effort=None, timeout_seconds=5, force=False, as_json=True,
    )
    assert cli._consult(Namespace(**base, round=0)) == 2
    first = json.loads(capsys.readouterr().out)
    assert first["next_step"] == "REVISE_BULK_WORKER_AND_RECHECK"
    assert first["feedback_round"] == 1

    assert cli._consult(Namespace(**base, round=1)) == 2
    second = json.loads(capsys.readouterr().out)
    assert second["next_step"] == "HUMAN_REVIEW"
    assert second["escalation_exhausted"] is True


def test_control_plane_parser_exposes_durable_lifecycle_commands() -> None:
    parser = cli._parser()

    serve = parser.parse_args(["serve", "--port", "8799"])
    submit = parser.parse_args([
        "submit",
        "--task",
        "task.json",
        "--idempotency-key",
        "idem-1",
        "--priority",
        "5",
        "--deadline-at",
        "2027-08-23T00:00:00Z",
    ])
    watch = parser.parse_args(["watch", "task-1", "--once"])
    cancel = parser.parse_args(["cancel", "task-1"])
    resume = parser.parse_args(["resume", "task-1"])
    agents = parser.parse_args(["agents", "--revoke", "worker-a"])
    worker = parser.parse_args(["worker", "--agent-token", "scoped-secret", "--once"])
    chain = parser.parse_args(
        [
            "chain-submit",
            "--chain-id",
            "chain-1",
            "--step-id",
            "review",
            "--step-index",
            "1",
            "--task",
            "task.json",
            "--predecessor-task-id",
            "build-task",
            "--allow-predecessor-state",
            "succeeded",
            "--defer-until-ready",
        ]
    )
    inspect_chain = parser.parse_args(["inspect-chain", "chain-1"])
    watch_chain = parser.parse_args(["watch-chain", "chain-1", "--once"])

    assert serve.command == "serve" and serve.port == 8799
    assert submit.command == "submit" and submit.idempotency_key == "idem-1"
    assert submit.priority == 5 and submit.deadline_at == "2027-08-23T00:00:00Z"
    assert watch.command == "watch" and watch.once is True
    assert cancel.command == "cancel"
    assert resume.command == "resume"
    assert agents.revoke == "worker-a"
    assert worker.agent_token == "scoped-secret"
    assert chain.command == "chain-submit" and chain.step_index == 1 and chain.defer_until_ready is True
    assert inspect_chain.command == "inspect-chain" and inspect_chain.chain_id == "chain-1"
    assert watch_chain.command == "watch-chain" and watch_chain.once is True


def test_chain_terminal_detection_respects_pending_and_step_states() -> None:
    completed = {
        "pending_steps": [],
        "steps": [{"envelope": {"state": "succeeded"}}],
    }
    waiting = {
        "pending_steps": [{"status": "pending"}],
        "steps": [{"envelope": {"state": "succeeded"}}],
    }
    assert cli._chain_terminal(completed) is True
    assert cli._chain_terminal(waiting) is False


def test_delegate_uses_claude_lane_adapter(monkeypatch, tmp_path: Path, capsys) -> None:
    task_path = tmp_path / "task.json"
    task_path.write_text(
        json.dumps({
            "task_id": "claude-cli",
            "objective": "Change one value.",
            "allowed_files": ["value.py"],
        }),
        encoding="utf-8",
    )
    expected = DelegationResult(
        task_id="claude-cli",
        status=ResultStatus.SUCCESS,
        summary="Claude sandbox verified",
    )
    calls = []

    def fake_run(task, repo):
        calls.append((task, repo))
        return expected

    monkeypatch.setattr(cli, "run_claude_task", fake_run)
    args = cli._parser().parse_args(
        [
            "delegate",
            "--backend",
            "claude-task",
            "--task",
            str(task_path),
            "--repo",
            str(tmp_path),
            "--json",
        ]
    )

    assert cli._delegate(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "SUCCESS"
    assert calls[0][0].task_id == "claude-cli"


def test_checked_lanes_exit_nonzero_for_unavailable_lane(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli,
        "lane_health_manifest",
        lambda *, probe: [
            {"name": "local-qwen", "status": "ready"},
            {"name": "claude-task", "status": "blocked"},
        ],
    )
    args = Namespace(check=True)

    assert cli._lanes(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_status_policy"].startswith("nonzero only")


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
