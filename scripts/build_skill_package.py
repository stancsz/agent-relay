"""Build the deterministic Codex skill archive and wheel-embedded copy."""

from __future__ import annotations

from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "skills" / "agent-relay"
OUTPUTS = (
    ROOT / "agent-relay.skill",
    ROOT / "src" / "agent_relay" / "_skill" / "agent-relay.skill",
)


def build(output: Path) -> None:
    files = sorted(path for path in SOURCE.rglob("*") if path.is_file())
    if not files:
        raise SystemExit(f"skill source is empty: {SOURCE}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for source in files:
            relative = source.relative_to(SOURCE).as_posix()
            info = zipfile.ZipInfo(f"agent-relay/{relative}", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            package.writestr(info, source.read_bytes())


def main() -> int:
    for output in OUTPUTS:
        build(output)
        print(f"Built {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
