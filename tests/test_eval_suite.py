import json
from pathlib import Path

import pytest

from evals.runner import (
    _aggregate,
    _apply_economics_attestations,
    _economics_report,
    _gate_status,
    _validate_declared_fixture_patches,
    run_suite,
)


def test_resumed_records_receive_economics_attestations() -> None:
    records = [
        {
            "id": "one",
            "passed": True,
            "status": "SUCCESS",
            "scope_reviewed": None,
            "scope_violation": False,
            "substantial_codex_repair": None,
        }
    ]

    _apply_economics_attestations(
        records,
        {
            "tasks": {
                "one": {
                    "scope_reviewed": True,
                    "scope_violation": False,
                    "substantial_codex_repair": True,
                }
            }
        },
    )

    assert records[0]["scope_reviewed"] is True
    assert records[0]["scope_review_basis"] == "economics attestation"
    assert records[0]["scope_violation"] is False
    assert records[0]["substantial_codex_repair"] is True


def test_fixture_suite_passes(tmp_path: Path) -> None:
    report = run_suite(
        backend="fixture",
        model=None,
        suite="bounded-basic",
        repo_root=Path.cwd(),
    )
    assert report["status"] == "PASS"
    assert report["metrics"]["bounded_acceptance_rate"] == 1.0
    assert report["metrics"]["blocked_task_correctness"] == 1.0
    assert report["metrics"]["codex_tool_execution_share"] == 0.0
    assert report["metrics"]["substantial_codex_repair_rate"] is None
    assert report["mvp_gate"]["checks"]["scope_violation_rate"] is True
    assert len(report["cases"]) == 11


def test_fixture_preflight_rejects_stale_insert_after_oracle() -> None:
    suite = json.loads(Path("evals/cases/bounded-50.json").read_text(encoding="utf-8"))
    case = next(
        item for item in suite["cases"]
        if item["id"] == "benchmark-test-normalize-email"
    )
    broken = json.loads(json.dumps(case))
    broken["task"]["context"] = ["tests/test_helpers.py:48-49"]

    with pytest.raises(ValueError, match="oracle hunk does not match"):
        _validate_declared_fixture_patches(
            [broken],
            cases_root=Path("evals/cases"),
            fixtures_root=Path("evals/fixtures"),
        )


def test_fixture_suite_compact_handoff_writes_patch_artifacts(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "patches"
    report = run_suite(
        backend="fixture",
        model=None,
        suite="bounded-basic",
        repo_root=Path.cwd(),
        compact=True,
        artifact_dir=artifact_dir,
    )

    assert report["status"] == "PASS"
    assert report["review_mode"] == "compact-handoff"
    assert report["frontier_handoff_tokens_estimate"] > 0
    assert report["full_evidence_artifact"] == "full-records.json"
    assert report["all_main_worktrees_unchanged"] is True
    assert all("patch" not in case for case in report["cases"])
    assert all(
        (artifact_dir / case["patch_artifact"]).is_file()
        for case in report["cases"]
    )


def test_suite_checkpoint_contains_complete_full_records(tmp_path: Path) -> None:
    checkpoint = tmp_path / "bounded-basic.checkpoint.json"
    report = run_suite(
        backend="fixture",
        model=None,
        suite="bounded-basic",
        repo_root=Path.cwd(),
        checkpoint_path=checkpoint,
    )

    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert report["run_state"] == "COMPLETE"
    assert saved["run_state"] == "COMPLETE"
    assert saved["completed_cases"] == saved["total_cases"] == 11
    assert len(saved["cases"]) == 11
    assert saved["report"]["run_state"] == "COMPLETE"


def test_suite_checkpoint_can_resume_in_bounded_chunks(tmp_path: Path) -> None:
    checkpoint = tmp_path / "bounded-basic.resume.json"
    first = run_suite(
        backend="fixture",
        model=None,
        suite="bounded-basic",
        repo_root=Path.cwd(),
        checkpoint_path=checkpoint,
        max_cases=3,
    )

    assert first["run_state"] == "PARTIAL"
    assert first["mvp_gate"]["checks"]["run_completed"] is False
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["completed_cases"] == 3

    final = run_suite(
        backend="fixture",
        model=None,
        suite="bounded-basic",
        repo_root=Path.cwd(),
        checkpoint_path=checkpoint,
        resume=True,
    )

    assert final["run_state"] == "COMPLETE"
    assert final["status"] == "PASS"
    assert len(final["cases"]) == 11


def test_fixture_suite_aggregate_proof_keeps_failure_index_and_evidence(
    tmp_path: Path,
) -> None:
    report = run_suite(
        backend="fixture",
        model=None,
        suite="bounded-basic",
        repo_root=Path.cwd(),
        aggregate=True,
        artifact_dir=tmp_path / "patches",
        sample=2,
    )

    assert report["status"] == "PASS"
    assert report["review_mode"] == "aggregate-proof"
    assert report["cases"] == []
    assert len(report["case_index"]["passed"]) == 11
    assert report["case_index"]["failed"] == []
    assert report["case_failures"] == []
    assert report["review_sample_count"] == 2
    assert len(report["case_review_sample"]) == 2
    assert report["all_main_worktrees_unchanged"] is True
    assert report["review_policy"]
    assert report["frontier_budget"]["full_report_tokens_estimate"] > 0
    assert report["frontier_budget"]["compact_handoff_tokens_estimate"] == report[
        "frontier_handoff_tokens_estimate"
    ]
    assert report["frontier_budget"]["response_compaction_reduction_estimate"] == pytest.approx(
        (
            report["frontier_budget"]["full_report_tokens_estimate"]
            - report["frontier_handoff_tokens_estimate"]
        )
        / report["frontier_budget"]["full_report_tokens_estimate"]
    )
    assert report["frontier_budget"]["selected_review_reduction_estimate"] == pytest.approx(
        (
            report["frontier_budget"]["full_report_tokens_estimate"]
            - report["frontier_budget"]["frontier_tokens_with_selected_review_estimate"]
        )
        / report["frontier_budget"]["full_report_tokens_estimate"]
    )


def test_economics_report_computes_net_savings_and_leverage() -> None:
    records = [
        {"id": "one", "duration_seconds": 12.0},
        {"id": "two", "duration_seconds": 8.0},
    ]
    report = _economics_report(records, {
        "source": "estimate",
        "cohort": {
            "suite": "test-suite",
            "fixture_digest": "fixture-digest",
            "repository_identity": "repo-identity",
            "model": "test-model",
            "backend": "ollama",
        },
        "tasks": {
            "one": {
                "baseline_codex_tokens": 1000,
                "delegation_codex_tokens": 100,
                "review_codex_tokens": 100,
                "repair_codex_tokens": 0,
                "recovery_codex_tokens": 0,
                "baseline_seconds": 10,
                "delegation_seconds": 0,
                "review_seconds": 0,
                "repair_seconds": 0,
                "recovery_seconds": 0,
                "scope_reviewed": True,
                "substantial_codex_repair": False,
            },
            "two": {
                "baseline_codex_tokens": 1000,
                "delegation_codex_tokens": 100,
                "review_codex_tokens": 100,
                "repair_codex_tokens": 0,
                "recovery_codex_tokens": 0,
                "baseline_seconds": 10,
                "delegation_seconds": 0,
                "review_seconds": 0,
                "repair_seconds": 0,
                "recovery_seconds": 0,
                "scope_reviewed": True,
                "substantial_codex_repair": False,
            },
        },
    })
    assert report["status"] == "ESTIMATED"
    assert report["net_codex_tokens_saved"] == 1600
    assert report["net_codex_token_reduction"] == 0.8
    assert report["frontier_token_leverage"] == 4.0
    assert report["delegation_codex_tokens"] == 200
    assert report["review_codex_tokens"] == 200
    assert report["repair_codex_tokens"] == 0
    assert report["recovery_codex_tokens"] == 0
    assert report["wall_clock_overhead"] == 0.0


def test_economics_report_charges_triage_decision_cost_when_recorded() -> None:
    report = _economics_report(
        [{"id": "one", "duration_seconds": 12.0}],
        {
            "source": "estimate",
            "cohort": {
                "suite": "test-suite",
                "fixture_digest": "fixture-digest",
                "repository_identity": "repo-identity",
                "model": "test-model",
                "backend": "ollama",
            },
            "tasks": {
                "one": {
                    "baseline_codex_tokens": 1000,
                    "triage_codex_tokens": 50,
                    "delegation_codex_tokens": 100,
                    "review_codex_tokens": 100,
                    "repair_codex_tokens": 0,
                    "recovery_codex_tokens": 0,
                    "baseline_seconds": 10,
                    "triage_seconds": 1,
                    "delegation_seconds": 0,
                    "review_seconds": 0,
                    "repair_seconds": 0,
                    "recovery_seconds": 0,
                    "scope_reviewed": True,
                    "substantial_codex_repair": False,
                },
            },
        },
    )

    assert report["triage_codex_tokens"] == 50
    assert report["delegated_codex_tokens"] == 250
    assert report["net_codex_token_reduction"] == 0.75
    assert report["codex_triage_seconds"] == 1
    assert "warnings" not in report


def test_suite_uses_repo_eval_root(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="--repo must contain evals"):
        run_suite(
            backend="fixture",
            model=None,
            suite="bounded-basic",
            repo_root=tmp_path,
        )


def test_aggregate_uses_case_oracle_and_manual_scope_review() -> None:
    cases = [
        {"id": "one", "eligibility": "eligible"},
        {"id": "two", "eligibility": "eligible"},
    ]
    records = [
        {
            "id": "one",
            "eligibility": "eligible",
            "passed": False,
            "attempts": 1,
            "verification_passed": True,
            "substantial_codex_repair": None,
            "scope_violation": True,
            "scope_reviewed": True,
        },
        {
            "id": "two",
            "eligibility": "eligible",
            "passed": True,
            "attempts": 1,
            "verification_passed": True,
            "substantial_codex_repair": None,
            "scope_violation": False,
            "scope_reviewed": True,
        },
    ]
    metrics = _aggregate(cases, records)
    assert metrics["bounded_acceptance_rate"] == 0.5
    assert metrics["verification_pass_rate"] == 0.5
    assert metrics["scope_violation_rate"] == 0.5
    assert metrics["scope_review_complete"] is True


def test_economics_requires_repair_classification() -> None:
    report = _economics_report(
        [{"id": "one", "duration_seconds": 0.0}],
        {
            "source": "estimate",
            "cohort": {
                "suite": "test-suite",
                "fixture_digest": "fixture-digest",
                "repository_identity": "repo-identity",
                "model": "test-model",
                "backend": "ollama",
            },
            "tasks": {"one": {
            "baseline_codex_tokens": 100,
            "delegation_codex_tokens": 10,
            "review_codex_tokens": 10,
            "repair_codex_tokens": 0,
            "recovery_codex_tokens": 0,
            "baseline_seconds": 10,
            "delegation_seconds": 1,
            "review_seconds": 1,
            "repair_seconds": 0,
            "recovery_seconds": 0,
            "scope_reviewed": True,
        }}},
    )
    assert report["status"] == "INCOMPLETE"
    assert "substantial_codex_repair" in report["missing"][0]


def test_matched_economics_requires_backend_identity() -> None:
    report = _economics_report(
        [],
        {
            "source": "estimate",
            "cohort": {
                "suite": "test-suite",
                "fixture_digest": "fixture-digest",
                "repository_identity": "repo-identity",
                "model": "test-model",
            },
            "tasks": {},
        },
        {
            "backend": "codex-ollama",
            "suite": "test-suite",
            "fixture_digest": "fixture-digest",
            "repository_identity": "repo-identity",
            "model": "test-model",
        },
    )

    assert report["status"] == "INCOMPLETE"
    assert "backend" in report["missing"]


def test_economics_rejects_extra_task_ids() -> None:
    report = _economics_report(
        [{"id": "one", "duration_seconds": 1.0}],
        {
            "source": "estimate",
            "cohort": {
                "suite": "test-suite",
                "fixture_digest": "fixture-digest",
                "repository_identity": "repo-identity",
                "model": "test-model",
                "backend": "ollama",
            },
            "tasks": {
                "one": {
                    "baseline_codex_tokens": 100,
                    "delegation_codex_tokens": 10,
                    "review_codex_tokens": 10,
                    "repair_codex_tokens": 0,
                    "recovery_codex_tokens": 0,
                    "baseline_seconds": 10,
                    "delegation_seconds": 1,
                    "review_seconds": 1,
                    "repair_seconds": 0,
                    "recovery_seconds": 0,
                    "scope_reviewed": True,
                    "substantial_codex_repair": False,
                },
                "unexpected": {},
            },
        },
    )

    assert report["status"] == "INVALID"
    assert report["extra_tasks"] == ["unexpected"]


def test_matched_economics_requires_exact_cohort_key() -> None:
    records = [{"id": "one", "duration_seconds": 1.0}]
    expected = {
        "suite": "test-suite",
        "fixture_digest": "fixture-digest",
        "repository_identity": "repo-identity",
        "model": "test-model",
        "backend": "codex-ollama",
        "cohort_key": "expected-key",
    }
    task = {
        "baseline_codex_tokens": 100,
        "delegation_codex_tokens": 10,
        "review_codex_tokens": 10,
        "repair_codex_tokens": 0,
        "recovery_codex_tokens": 0,
        "baseline_seconds": 10,
        "delegation_seconds": 1,
        "review_seconds": 1,
        "repair_seconds": 0,
        "recovery_seconds": 0,
        "scope_reviewed": True,
        "substantial_codex_repair": False,
    }
    report = _economics_report(
        records,
        {
            "source": "estimate",
            "cohort": {**expected, "cohort_key": "wrong-key"},
            "tasks": {"one": task},
        },
        expected,
    )

    assert report["status"] == "INVALID"
    assert report["mismatches"]["cohort_key"]["expected"] == "expected-key"


def test_codex_telemetry_requires_provenance_and_usage() -> None:
    cohort = {
        "suite": "test-suite",
        "fixture_digest": "fixture-digest",
        "repository_identity": "repo-identity",
        "model": "test-model",
        "backend": "codex-ollama",
        "cohort_key": "cohort-key",
    }
    report = _economics_report(
        [{"id": "one", "duration_seconds": 1.0}],
        {
            "source": "codex-telemetry",
            "cohort": cohort,
            "tasks": {"one": {}},
        },
        cohort,
    )

    assert report["status"] == "INCOMPLETE"
    assert "provenance" in report["missing"]


def test_aborted_run_cannot_pass_mvp_gate() -> None:
    metrics = {
        "blocked_expected_tasks": 1,
        "blocked_task_correctness": 1.0,
        "bounded_acceptance_rate": 1.0,
        "verification_pass_rate": 1.0,
        "scope_violation_rate": 0.0,
        "scope_review_complete": True,
        "substantial_codex_repair_rate": 0.0,
    }
    economics = {
        "status": "MEASURED",
        "net_codex_token_reduction": 0.8,
        "wall_clock_overhead": 0.1,
    }

    gate = _gate_status(metrics, economics, 50, run_state="ABORTED")

    assert gate["overall"] == "FAIL"
    assert gate["checks"]["run_completed"] is False
