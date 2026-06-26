import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from bot import env_config


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setenv("ENV_FILE", str(path))
    return path


def test_resolve_env_file_path_prefers_explicit(tmp_path, monkeypatch):
    explicit = tmp_path / "custom.env"
    monkeypatch.setenv("ENV_FILE", str(explicit))
    assert env_config.resolve_env_file_path() == explicit


def test_resolve_env_file_path_uses_data_in_docker(monkeypatch):
    monkeypatch.delenv("ENV_FILE", raising=False)
    monkeypatch.setenv("RUN_MODE", "docker")
    assert env_config.resolve_env_file_path() == Path("/data/.env")


def test_mask_env_value_hides_secrets():
    assert env_config.mask_env_value("LLM_API_KEY", "abcdefghijklmnop") == "abcd…mnop"
    assert env_config.mask_env_value("LLM_MODEL", "google/gemini") == "google/gemini"


def test_set_env_value_updates_file_atomically(env_file):
    env_file.write_text("# comment\nLLM_MODEL=old-model\n", encoding="utf-8")

    restart_required, message = env_config.set_env_value(
        "LLM_MODEL", "google/gemini-flash-2.5"
    )

    assert restart_required is False
    assert "Updated LLM_MODEL" in message
    saved = env_file.read_text(encoding="utf-8")
    assert "# comment" in saved
    assert "LLM_MODEL=google/gemini-flash-2.5" in saved
    assert os.environ["LLM_MODEL"] == "google/gemini-flash-2.5"


def test_set_env_value_preserves_file_mode(env_file):
    env_file.write_text("LLM_MODEL=old\n", encoding="utf-8")
    env_file.chmod(0o600)

    env_config.set_env_value("LLM_MODEL", "new-model")

    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_set_env_value_blocks_protected_keys(env_file):
    ok, message = env_config.set_env_value("COOKIES_DIR", "/tmp/cookies")
    assert ok is False
    assert "cannot be changed" in message
    assert not env_file.exists()


def test_ensure_env_file_seeded_creates_writable_file(env_file, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "google/gemini-flash-2.5")

    env_config.ensure_env_file_seeded()

    assert env_file.exists()
    assert "LLM_MODEL=google/gemini-flash-2.5" in env_file.read_text(encoding="utf-8")


def test_apply_env_to_runtime_updates_llm_model(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "old")
    import config
    import bot.llm as llm

    config.LLM_MODEL = "old"
    llm.LLM_MODEL = "old"

    restart_required = env_config.apply_env_to_runtime(
        "LLM_MODEL", "google/gemini-flash-2.5"
    )

    assert restart_required is False
    assert config.LLM_MODEL == "google/gemini-flash-2.5"
    assert llm.LLM_MODEL == "google/gemini-flash-2.5"


def test_format_env_panel_lists_masked_values(env_file):
    env_file.write_text(
        "LLM_API_KEY=supersecretvalue\nLLM_MODEL=google/gemini\n",
        encoding="utf-8",
    )

    panel = env_config.format_env_panel()

    assert str(env_file) in panel
    assert "LLM_MODEL=google/gemini" in panel
    assert "super" not in panel
