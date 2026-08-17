from __future__ import annotations

import json
from pathlib import Path

from evals.scope_review import review_task_patch
from local_code_delegate.task import DelegationTask


ROOT = Path(__file__).resolve().parents[1]


def _case(case_id: str) -> dict:
    value = json.loads((ROOT / "evals/cases/bounded-50.json").read_text(encoding="utf-8"))
    return next(case for case in value["cases"] if case["id"] == case_id)


def _oracle(case: dict) -> str:
    return (ROOT / "evals/cases" / case["patch_file"]).read_text(encoding="utf-8")


def _review(case_id: str, patch: str | None = None) -> dict:
    case = _case(case_id)
    task = DelegationTask.from_dict(case["task"])
    return review_task_patch(
        _oracle(case) if patch is None else patch,
        task,
        repository=ROOT / "evals/fixtures" / case["fixture"],
        expected_files=case["expected_files"],
    )


def test_replace_patch_is_reviewed_against_declared_context() -> None:
    result = _review("benchmark-require-nonempty")

    assert result["reviewed"] is True
    assert result["violation"] is False


def test_insert_after_patch_is_reviewed_against_declared_anchor() -> None:
    result = _review("benchmark-test-normalize-email")

    assert result["reviewed"] is True
    assert result["violation"] is False


def test_insert_after_wrong_location_is_a_scope_violation() -> None:
    wrong_location = """diff --git a/tests/test_helpers.py b/tests/test_helpers.py
--- a/tests/test_helpers.py
+++ b/tests/test_helpers.py
@@ -47,3 +47,6 @@
 def test_make_tag():
     assert make_tag("item") == "<item>"
+
+def test_normalize_email_empty_input():
+    assert normalize_email("") == ""
"""

    result = _review("benchmark-test-normalize-email", wrong_location)

    assert result["reviewed"] is True
    assert result["violation"] is True
    assert any("anchored" in reason for reason in result["reasons"])
