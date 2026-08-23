"""Verify that the checked-in skill archive matches its source directory."""

from __future__ import annotations

import sys
from pathlib import Path
import zipfile


def validate(archive: Path, source_dir: Path) -> list[str]:
    expected = {
        f"agent-relay/{path.relative_to(source_dir).as_posix()}": path
        for path in sorted(source_dir.rglob("*"))
        if path.is_file()
    }
    errors: list[str] = []
    if not archive.is_file():
        return [f"missing archive {archive}"]
    try:
        with zipfile.ZipFile(archive) as package:
            actual = set(package.namelist())
            if actual != set(expected):
                errors.append(
                    f"archive entries differ: expected {sorted(expected)}, "
                    f"got {sorted(actual)}"
                )
            for name, source in expected.items():
                if name in actual and package.read(name) != source.read_bytes():
                    errors.append(f"archive content differs for {name}")
    except (OSError, zipfile.BadZipFile) as exc:
        errors.append(f"invalid skill archive: {exc}")
    return errors


def main() -> int:
    archive = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("agent-relay.skill")
    source_dir = (
        Path(sys.argv[2]) if len(sys.argv) > 2 else Path("skills/agent-relay")
    )
    errors = validate(archive, source_dir)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Skill archive matches source: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
