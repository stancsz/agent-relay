#!/usr/bin/env python3
"""Deterministic tests for validate_goal_docs.py."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from validate_goal_docs import MARKER_END, MARKER_START, validate


def wrapped(body: str) -> str:
    return f"Human-owned preface.\n\n{MARKER_START}\n{body.strip()}\n{MARKER_END}\n"


ROADMAP = wrapped(
    """
## Goal Loop Roadmap

| roadmap_id | outcome | dependencies | status | evidence_gate |
|---|---|---|---|---|
| R-001 | Ship bounded feature | none | active | E-001 |
"""
)

GOAL_VALID = wrapped(
    """
## Goal Loop Control

- goal_id: GL-test

## Claude Dispatch Ledger

| dispatch_id | parent_id | role | instance_id | job_id | roadmap_id | scope | status | started_at | last_seen_at | checkpoint |
|---|---|---|---|---|---|---|---|---|---|---|
| GL-test-O1 | codex | orchestrator | lead-1 | job-1 | R-001 | coordinate | running | t0 | t1 | CP-1 |
| GL-test-S1 | GL-test-O1 | subagent | sub-1 | task-1 | R-001 | code | running | t0 | t1 | CP-1 |
| GL-test-S2 | GL-test-O1 | subagent | sub-2 | task-2 | R-001 | tests | running | t0 | t1 | CP-1 |
| GL-test-S3 | GL-test-O1 | subagent | sub-3 | task-3 | R-001 | review | verifying | t0 | t1 | CP-1 |
| GL-test-S0 | GL-test-O1 | subagent | sub-0 | task-0 | R-001 | prior | accepted | t0 | t1 | CP-0 |
"""
)

EVAL_VALID = wrapped(
    """
## Goal Loop Evaluation

| criterion_id | requirement | verifier | evidence_required | status |
|---|---|---|---|---|
| E-001 | Feature works | Codex | tests | unproven |

## Dispatch Evaluations

| dispatch_id | receipt | changed_paths | verification | codex_verdict | notes |
|---|---|---|---|---|---|
| GL-test-S0 | job-0 | src/a.py | pytest 0 | accepted | reviewed |
"""
)


class GoalDocsTests(unittest.TestCase):
    def write_fixture(self, root: Path, goal: str = GOAL_VALID) -> None:
        (root / "ROADMAP.md").write_text(ROADMAP, encoding="utf-8")
        (root / "GOAL.md").write_text(goal, encoding="utf-8")
        (root / "EVAL.md").write_text(EVAL_VALID, encoding="utf-8")

    def test_valid_documents_and_three_subagent_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            self.write_fixture(root)
            result = validate(root)
            self.assertTrue(result["valid"], result["issues"])
            self.assertEqual(result["active_orchestrators"], 1)
            self.assertEqual(result["active_subagents"], 3)

    def test_rejects_excess_concurrency_and_missing_evaluation(self) -> None:
        extra = GOAL_VALID.replace(
            MARKER_END,
            "| GL-test-O2 | codex | orchestrator | lead-2 | job-2 | R-001 | duplicate | queued | t0 | t1 | CP-1 |\n"
            "| GL-test-S4 | GL-test-O1 | subagent | sub-4 | task-4 | R-001 | extra | queued | t0 | t1 | CP-1 |\n"
            "| GL-test-S5 | GL-test-O1 | subagent | sub-5 | task-5 | R-001 | failed | failed | t0 | t1 | CP-1 |\n"
            + MARKER_END,
        )
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            self.write_fixture(root, goal=extra)
            result = validate(root)
            self.assertFalse(result["valid"])
            joined = "\n".join(result["issues"])
            self.assertIn("active orchestrator count is 2", joined)
            self.assertIn("active subagent count is 4", joined)
            self.assertIn("GL-test-S5", joined)

    def test_rejects_unresolved_identity_and_wrong_parent(self) -> None:
        invalid = GOAL_VALID.replace(
            "| GL-test-S1 | GL-test-O1 | subagent | sub-1 | task-1 |",
            "| GL-test-S1 | wrong-lead | subagent | unresolved | pending |",
        )
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            self.write_fixture(root, goal=invalid)
            result = validate(root)
            self.assertFalse(result["valid"])
            joined = "\n".join(result["issues"])
            self.assertIn("GL-test-S1 has unresolved instance_id", joined)
            self.assertIn("GL-test-S1 has unresolved job_id", joined)
            self.assertIn("GL-test-S1", joined)

    def test_rejects_subagents_without_active_orchestrator(self) -> None:
        invalid = GOAL_VALID.replace(
            "| GL-test-O1 | codex | orchestrator | lead-1 | job-1 | R-001 | coordinate | running | t0 | t1 | CP-1 |\n",
            "",
        )
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            self.write_fixture(root, goal=invalid)
            result = validate(root)
            self.assertFalse(result["valid"])
            self.assertIn("active subagents exist without an active orchestrator", "\n".join(result["issues"]))

    def test_requires_eval_file(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            (root / "ROADMAP.md").write_text(ROADMAP, encoding="utf-8")
            (root / "GOAL.md").write_text(GOAL_VALID, encoding="utf-8")
            result = validate(root)
            self.assertFalse(result["valid"])
            self.assertIn("EVAL.md: missing", "\n".join(result["issues"]))
            self.assertTrue(str(result["paths"]["eval"]).endswith("EVAL.md"))


if __name__ == "__main__":
    unittest.main()
