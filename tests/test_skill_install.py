from pathlib import Path
import zipfile

import pytest

from agent_relay.skill import install_skill


def test_install_skill_extracts_embedded_archive(tmp_path: Path) -> None:
    destination = tmp_path / "codex" / "skills" / "agent-relay"

    installed = install_skill(destination=destination)

    assert installed == destination
    assert (destination / "SKILL.md").is_file()
    assert (destination / "agents" / "openai.yaml").is_file()
    assert (destination / "references" / "qwen-worker.md").is_file()


def test_install_skill_requires_force_for_existing_destination(tmp_path: Path) -> None:
    destination = tmp_path / "agent-relay"
    destination.mkdir(parents=True)
    (destination / "marker.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError):
        install_skill(destination=destination)

    install_skill(destination=destination, force=True)
    assert (destination / "marker.txt").read_text(encoding="utf-8") == "keep"
    assert (destination / "SKILL.md").is_file()


def test_install_skill_rejects_unsafe_archive(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.skill"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("agent-relay/../escape.txt", "no")
        package.writestr("agent-relay/SKILL.md", "---\nname: agent-relay\n---\n")

    with pytest.raises(ValueError, match="unsafe path"):
        install_skill(destination=tmp_path / "destination", archive=archive)
