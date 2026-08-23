from pathlib import Path

from agent_relay import env as env_module


def test_load_dotenv_populates_review_model(monkeypatch, tmp_path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        '\n'.join([
            '# project defaults',
            'AR_CODEX_REVIEW_MODEL="gpt-5.6-luna"',
            "AR_AGY_MODEL=gemini-ar",
            " export AR_CODEX_MODEL=qwen-ar",
        ]),
        encoding="utf-8",
    )
    monkeypatch.delenv("AR_CODEX_REVIEW_MODEL", raising=False)
    monkeypatch.delenv("AR_AGY_MODEL", raising=False)
    monkeypatch.delenv("AR_CODEX_MODEL", raising=False)

    env_module.load_dotenv(path=dotenv, force=True)

    assert env_module.os.environ["AR_CODEX_REVIEW_MODEL"] == "gpt-5.6-luna"
    assert env_module.os.environ["AR_AGY_MODEL"] == "gemini-ar"
    assert env_module.os.environ["AR_CODEX_MODEL"] == "qwen-ar"


def test_load_dotenv_keeps_existing_environment_variables(monkeypatch, tmp_path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("AR_CODEX_REVIEW_MODEL=gpt-5.6-luna", encoding="utf-8")
    monkeypatch.setenv("AR_CODEX_REVIEW_MODEL", "from-env")
    env_module.load_dotenv(path=dotenv, force=True)
    assert env_module.os.environ["AR_CODEX_REVIEW_MODEL"] == "from-env"
