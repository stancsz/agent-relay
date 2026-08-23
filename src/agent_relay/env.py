from __future__ import annotations

from pathlib import Path
import os
from threading import Lock


_ENV_LOCK = Lock()
_ENV_LOADED = False


def _parse_dotenv_line(raw_line: str) -> tuple[str, str] | None:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export ") :].strip()
    if "=" not in line:
        return None
    key, value = line.split("=", 1)
    key = key.strip()
    if not key:
        return None
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        value = value[1:-1]
    return key, value


def _load_dotenv_file(dotenv_path: Path) -> None:
    if not dotenv_path.is_file():
        return
    try:
        text = dotenv_path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        parsed = _parse_dotenv_line(line)
        if parsed is None:
            continue
        key, value = parsed
        os.environ.setdefault(key, value)


def load_dotenv(path: str | Path | None = None, *, force: bool = False) -> None:
    """Load `~/.env` (or a provided file) into environment variables."""

    dotenv_path = Path.home() / ".env" if path is None else Path(path)
    if force:
        _load_dotenv_file(dotenv_path)
        return

    global _ENV_LOADED
    if _ENV_LOADED:
        return
    with _ENV_LOCK:
        if _ENV_LOADED:
            return
        _load_dotenv_file(dotenv_path)
        _ENV_LOADED = True
