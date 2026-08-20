"""Small dependency-free CI check for the canonical Agent Relay skill."""

from __future__ import annotations

import re
import sys
from pathlib import Path


def validate(skill_dir: Path) -> list[str]:
    skill_file = skill_dir / "SKILL.md"
    errors: list[str] = []
    if not skill_file.is_file():
        return [f"missing {skill_file}"]
    text = skill_file.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        errors.append("SKILL.md must start with YAML frontmatter")
    else:
        frontmatter = text.split("\n---\n", 1)[0]
        name_match = re.search(r"^name:\s*([^\s]+)\s*$", frontmatter, re.MULTILINE)
        description_match = re.search(
            r"^description:\s*.+$", frontmatter, re.MULTILINE
        )
        if name_match is None:
            errors.append("frontmatter is missing name")
        elif name_match.group(1) != skill_dir.name:
            errors.append(
                f"frontmatter name {name_match.group(1)!r} does not match "
                f"directory {skill_dir.name!r}"
            )
        if description_match is None:
            errors.append("frontmatter is missing description")
    if re.search(r"\b(?:TODO|FIXME|YOUR_[A-Z_]+)\b", text):
        errors.append("skill contains an unfinished placeholder")
    return errors


def main() -> int:
    skill_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("skills/agent-relay")
    errors = validate(skill_dir)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Skill is valid: {skill_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
