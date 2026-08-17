from pathlib import Path

from local_code_delegate.frontier import frontier_budget, task_manifest, token_estimate


def test_frontier_budget_counts_compact_packet_and_selected_artifacts(
    tmp_path: Path,
) -> None:
    (tmp_path / "one.patch").write_text("+" + ("x" * 399) + "\n", encoding="utf-8")
    compact = {"status": "PASS", "case_index": [{"id": "one", "passed": True}]}
    budget, final_tokens = frontier_budget(
        full_payload={"cases": [{"id": "one", "patch": "x" * 4000}]},
        compact_payload=compact,
        artifact_root=tmp_path,
        review_artifacts=["one.patch"],
        task_manifest=[{"id": "one"}],
    )

    assert final_tokens == token_estimate(compact)
    assert budget["full_report_tokens_estimate"] > final_tokens
    assert budget["review_artifact_tokens_estimate"] > 0
    assert budget["compact_handoff_tokens_estimate"] == final_tokens
    assert budget["frontier_tokens_with_selected_review_estimate"] > final_tokens
    assert budget["selected_review_reduction_estimate"] < budget[
        "response_compaction_reduction_estimate"
    ]
    assert budget["task_manifest_tokens_estimate"] > 0
    assert budget["response_compaction_reduction_estimate"] > 0.0


def test_token_estimate_is_deterministic_for_json_values() -> None:
    assert token_estimate({"b": 2, "a": 1}) == token_estimate({"a": 1, "b": 2})


def test_task_manifest_modes_make_decomposition_cost_explicit() -> None:
    cases = [{
        "id": "one",
        "task": {
            "task_id": "one",
            "objective": "Change one value.",
            "allowed_files": ["value.py"],
            "context": ["value.py", "test_value.py"],
            "requirements": ["The value is two."],
            "constraints": ["Do not change the API."],
            "verification": ["pytest -q"],
            "success_criteria": ["Tests pass."],
        },
    }]

    full = task_manifest(cases, "full")
    contract = task_manifest(cases, "contract")
    thin = task_manifest(cases, "thin")

    assert full is not None and full[0]["task"]["context"]
    assert contract is not None and "context" not in contract[0]["task"]
    assert thin is not None and "requirements" not in thin[0]["task"]
    assert task_manifest(cases, "none") is None
    assert token_estimate(thin) < token_estimate(contract)
