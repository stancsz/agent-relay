"""Install the bundled Agent Relay Codex skill."""

from __future__ import annotations

from importlib.resources import files
import io
import os
from pathlib import Path, PurePosixPath
import zipfile


SKILL_NAME = "agent-relay"
_ARCHIVE_RESOURCE = "_skill/agent-relay.skill"


def default_codex_home() -> Path:
    """Return the platform-neutral Codex home directory."""

    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _archive_bytes(archive: Path | None) -> bytes:
    if archive is not None:
        return archive.read_bytes()
    return files("agent_relay").joinpath(_ARCHIVE_RESOURCE).read_bytes()


def _safe_members(package: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, Path]]:
    expected_prefix = f"{SKILL_NAME}/"
    members: list[tuple[zipfile.ZipInfo, Path]] = []
    for info in package.infolist():
        name = info.filename
        if not name.startswith(expected_prefix):
            raise ValueError(f"skill archive contains an unexpected path: {name!r}")
        relative = PurePosixPath(name[len(expected_prefix) :])
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError(f"skill archive contains an unsafe path: {name!r}")
        if relative.is_absolute():
            raise ValueError(f"skill archive contains an absolute path: {name!r}")
        target = Path(*relative.parts)
        if info.is_dir():
            continue
        members.append((info, target))
    if not any(target.name == "SKILL.md" for _, target in members):
        raise ValueError("skill archive does not contain SKILL.md")
    return members


def install_skill(
    *,
    destination: Path | None = None,
    archive: Path | None = None,
    force: bool = False,
) -> Path:
    """Install the bundled or supplied skill into Codex's skill directory.

    Existing installations are left untouched unless ``force`` is explicit.
    Files are validated before any destination file is written.
    """

    target_root = (
        destination.expanduser()
        if destination is not None
        else default_codex_home() / "skills" / SKILL_NAME
    )
    payload = _archive_bytes(archive.expanduser() if archive is not None else None)
    with zipfile.ZipFile(io.BytesIO(payload)) as package:
        members = _safe_members(package)
        if target_root.exists() and not force:
            raise FileExistsError(
                f"skill destination already exists: {target_root} (use --force to update)"
            )
        target_root.mkdir(parents=True, exist_ok=True)
        for info, relative in members:
            output = target_root / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(package.read(info))
    return target_root
