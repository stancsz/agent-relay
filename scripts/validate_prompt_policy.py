"""Check that the standalone Claude lane copies the canonical prompt policy."""

from __future__ import annotations

import ast
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
POLICY_FILES = (
    ROOT / "src" / "agent_relay" / "prompt_policy.py",
    ROOT / "lanes" / "claude-task" / "scripts" / "prompt_policy.py",
)


def _literal_assignment(path: Path, name: str) -> object:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise ValueError(f"{path} does not define a literal {name}")


def _guidance(path: Path) -> str:
    value = _literal_assignment(path, "HIGH_AGENCY_GUIDANCE")
    if isinstance(value, str):
        return value
    raise ValueError(f"{path} HIGH_AGENCY_GUIDANCE is not a string")


def _version(path: Path) -> str:
    value = _literal_assignment(path, "PROMPT_POLICY_VERSION")
    if isinstance(value, str):
        return value
    raise ValueError(f"{path} PROMPT_POLICY_VERSION is not a string")


def validate() -> list[str]:
    errors: list[str] = []
    missing = [str(path) for path in POLICY_FILES if not path.is_file()]
    if missing:
        return [f"missing prompt policy file: {path}" for path in missing]
    values = [_guidance(path) for path in POLICY_FILES]
    versions = [_version(path) for path in POLICY_FILES]
    if len(set(values)) != 1:
        errors.append("canonical and standalone HIGH_AGENCY_GUIDANCE values differ")
    if len(set(versions)) != 1:
        errors.append("canonical and standalone PROMPT_POLICY_VERSION values differ")
    if not values[0].startswith("High-agency operating guidance:"):
        errors.append("HIGH_AGENCY_GUIDANCE has an unexpected header")
    return errors


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Prompt policy copies match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
