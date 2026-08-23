#!/usr/bin/env python3
"""Validate Goal Loop ROADMAP/GOAL/EVAL managed sections without modifying them."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


ACTIVE = {"queued", "running", "verifying"}
TERMINAL = {"blocked", "failed", "accepted", "rejected", "cancelled", "interrupted"}
ORCHESTRATOR_ROLES = {"orchestrator", "claude-orchestrator"}
WORKER_ROLES = {"subagent", "claude-worker", "claude-verifier"}
UNRESOLVED_IDENTITY = {"", "-", "pending", "unknown", "unresolved", "none", "null"}
MARKER_START = "<!-- goal-loop:managed:start -->"
MARKER_END = "<!-- goal-loop:managed:end -->"


def managed(text: str, path: Path, issues: list[str]) -> str:
    starts = text.count(MARKER_START)
    ends = text.count(MARKER_END)
    if starts != 1 or ends != 1:
        issues.append(f"{path.name}: expected exactly one managed marker pair")
        return ""
    before, rest = text.split(MARKER_START, 1)
    body, after = rest.split(MARKER_END, 1)
    if MARKER_END in before or MARKER_START in after:
        issues.append(f"{path.name}: malformed managed markers")
    return body


def table_rows(section: str, heading: str) -> list[dict[str, str]]:
    match = re.search(rf"^## {re.escape(heading)}\s*$([\s\S]*?)(?=^## |\Z)", section, re.MULTILINE)
    if not match:
        return []
    lines = [line.strip() for line in match.group(1).splitlines() if line.strip().startswith("|")]
    if len(lines) < 2:
        return []
    headers = [cell.strip() for cell in lines[0].strip("|").split("|")]
    rows: list[dict[str, str]] = []
    for line in lines[2:]:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) == len(headers):
            rows.append(dict(zip(headers, cells)))
    return rows


def validate(root: Path) -> dict[str, object]:
    issues: list[str] = []
    paths = {
        "roadmap": root / "ROADMAP.md",
        "goal": root / "GOAL.md",
        "eval": root / "EVAL.md",
    }
    sections: dict[str, str] = {}
    for key, path in paths.items():
        if not path.is_file():
            issues.append(f"{path.name}: missing")
            sections[key] = ""
            continue
        sections[key] = managed(path.read_text(encoding="utf-8"), path, issues)

    required = {
        "roadmap": ["Goal Loop Roadmap"],
        "goal": ["Goal Loop Control", "Claude Dispatch Ledger"],
        "eval": ["Goal Loop Evaluation", "Dispatch Evaluations"],
    }
    for key, headings in required.items():
        for heading in headings:
            if f"## {heading}" not in sections[key]:
                issues.append(f"{paths[key].name}: missing '{heading}' managed heading")

    dispatches = table_rows(sections["goal"], "Claude Dispatch Ledger")
    evaluations = table_rows(sections["eval"], "Dispatch Evaluations")
    roadmap_rows = table_rows(sections["roadmap"], "Goal Loop Roadmap")
    dispatch_ids = [row.get("dispatch_id", "") for row in dispatches]
    if any(not value for value in dispatch_ids):
        issues.append("GOAL dispatch ledger contains a row without dispatch_id")
    duplicates = sorted({value for value in dispatch_ids if value and dispatch_ids.count(value) > 1})
    if duplicates:
        issues.append(f"duplicate dispatch_id values: {', '.join(duplicates)}")

    active_orchestrators = [row for row in dispatches if row.get("role") in ORCHESTRATOR_ROLES and row.get("status") in ACTIVE]
    active_subagents = [row for row in dispatches if row.get("role") in WORKER_ROLES and row.get("status") in ACTIVE]
    if len(active_orchestrators) > 1:
        issues.append(f"active orchestrator count is {len(active_orchestrators)}; maximum is 1")
    if len(active_subagents) > 3:
        issues.append(f"active subagent count is {len(active_subagents)}; maximum is 3")
    if active_subagents and not active_orchestrators:
        issues.append("active subagents exist without an active orchestrator")
    active_roadmap = [row for row in roadmap_rows if row.get("status") == "active"]
    if len(active_roadmap) > 1:
        issues.append(f"active roadmap item count is {len(active_roadmap)}; maximum is 1")
    if (active_orchestrators or active_subagents) and len(active_roadmap) != 1:
        issues.append("active Claude dispatches require exactly one active roadmap item")
    if len(active_roadmap) == 1:
        active_roadmap_id = active_roadmap[0].get("roadmap_id", "")
        wrong_roadmap = sorted(
            row.get("dispatch_id", "")
            for row in active_orchestrators + active_subagents
            if row.get("roadmap_id", "") != active_roadmap_id
        )
        if wrong_roadmap:
            issues.append(f"active dispatches bound to the wrong roadmap_id: {', '.join(wrong_roadmap)}")
    active_rows = active_orchestrators + active_subagents
    for row in active_rows:
        # A queued dispatch is recorded before the bridge returns its native
        # identifiers. Require resolved identities once work is actually live.
        if row.get("status") == "queued":
            continue
        dispatch_id = row.get("dispatch_id", "<missing>")
        if row.get("instance_id", "").strip().lower() in UNRESOLVED_IDENTITY:
            issues.append(f"active dispatch {dispatch_id} has unresolved instance_id")
        if row.get("job_id", "").strip().lower() in UNRESOLVED_IDENTITY:
            issues.append(f"active dispatch {dispatch_id} has unresolved job_id")
    if len(active_orchestrators) == 1:
        orchestrator_id = active_orchestrators[0].get("dispatch_id", "")
        wrong_parent = sorted(
            row.get("dispatch_id", "")
            for row in active_subagents
            if row.get("parent_id", "") != orchestrator_id
        )
        if wrong_parent:
            issues.append(f"active subagents with wrong parent_id: {', '.join(wrong_parent)}")

    evaluation_ids = {row.get("dispatch_id", "") for row in evaluations}
    missing_evaluations = sorted(
        row.get("dispatch_id", "")
        for row in dispatches
        if row.get("status") in TERMINAL and row.get("dispatch_id", "") not in evaluation_ids
    )
    if missing_evaluations:
        issues.append(f"terminal dispatches missing EVAL rows: {', '.join(missing_evaluations)}")

    return {
        "valid": not issues,
        "root": str(root),
        "paths": {key: str(path) for key, path in paths.items()},
        "active_orchestrators": len(active_orchestrators),
        "active_subagents": len(active_subagents),
        "active_roadmap_items": len(active_roadmap),
        "dispatch_count": len(dispatches),
        "evaluation_count": len(evaluations),
        "issues": issues,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", help="repository root containing Goal Loop control documents")
    args = parser.parse_args()
    root = Path(args.repo).resolve()
    if not root.is_dir():
        parser.error("repo must be an existing directory")
    result = validate(root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
