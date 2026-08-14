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


def test_set_env_values_validates_every_field_before_writing(env_file):
    env_file.write_text("LLM_MODEL=old-model\n", encoding="utf-8")

    restart_required, message = env_config.set_env_values(
        {"LLM_MODEL": "new-model", "WEB_SETTINGS_PORT": "not-a-port"}
    )

    assert restart_required is False
    assert "WEB_SETTINGS_PORT" in message
    assert env_file.read_text(encoding="utf-8") == "LLM_MODEL=old-model\n"


def test_set_env_values_requires_an_https_web_settings_url(env_file):
    restart_required, message = env_config.set_env_values(
        {"WEB_SETTINGS_URL": "http://bot.example.test"}
    )

    assert restart_required is False
    assert "public HTTPS URL" in message
    assert not env_file.exists()


def test_set_env_value_updates_ponder_step_budget_at_runtime(env_file):
    env_file.write_text("LLM_PONDER_MAX_STEPS=10\n", encoding="utf-8")
    import config
    import bot.agent as agent

    restart_required, message = env_config.set_env_value("LLM_PONDER_MAX_STEPS", "14")

    assert restart_required is False
    assert "Updated LLM_PONDER_MAX_STEPS" in message
    assert "LLM_PONDER_MAX_STEPS=14" in env_file.read_text(encoding="utf-8")
    assert config.LLM_PONDER_MAX_STEPS == 14
    assert agent.LLM_PONDER_MAX_STEPS == 14


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("LLM_REASONING_EFFORT", "low", "low"),
        ("LLM_REASONING_EFFORT", "NONE", "none"),
        ("LLM_PONDER_REASONING_EFFORT", "MeDiuM", "medium"),
        ("LLM_VISION_REASONING_EFFORT", "HIGH", "high"),
        ("LLM_VISION_REASONING_EFFORT", "xhigh", "xhigh"),
        ("LLM_REASONING_EFFORT", "minimal", "minimal"),
    ],
)
def test_validate_env_value_normalizes_reasoning_effort(key, value, expected):
    assert env_config.validate_env_value(key, value) == (key, expected)


@pytest.mark.parametrize(
    "key",
    [
        "LLM_REASONING_EFFORT",
        "LLM_PONDER_REASONING_EFFORT",
        "LLM_VISION_REASONING_EFFORT",
    ],
)
def test_validate_env_value_accepts_empty_reasoning_effort(key):
    assert env_config.validate_env_value(key, "") == (key, "")


def test_validate_env_value_rejects_invalid_reasoning_effort():
    with pytest.raises(env_config.EnvUpdateError, match="must be one of"):
        env_config.validate_env_value("LLM_REASONING_EFFORT", "ultra")


@pytest.mark.parametrize("value", ["0", "21", "many"])
def test_set_env_value_rejects_invalid_ponder_step_budget(env_file, value):
    ok, message = env_config.set_env_value("LLM_PONDER_MAX_STEPS", value)

    assert ok is False
    assert "integer from 1 to 20" in message
    assert not env_file.exists()


@pytest.mark.parametrize(
    ("key", "valid", "invalid"),
    [
        ("YTDLP_MAX_CONCURRENT_DOWNLOADS", "2", "9"),
        ("YTDLP_QUEUE_TIMEOUT_SEC", "30", "0"),
        ("YTDLP_DOWNLOAD_TIMEOUT_SEC", "180", "29"),
        ("YTDLP_SOCKET_TIMEOUT_SEC", "20", "121"),
    ],
)
def test_validate_ytdlp_management_settings(key, valid, invalid):
    assert env_config.validate_env_value(key, valid) == (key, valid)
    with pytest.raises(env_config.EnvUpdateError, match="must be an integer"):
        env_config.validate_env_value(key, invalid)
    assert key in env_config.RESTART_REQUIRED_KEYS


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


def test_apply_env_to_runtime_updates_reasoning_effort(monkeypatch):
    import config
    import bot.llm as llm
    import bot.vision as vision

    config.LLM_REASONING_EFFORT = ""
    config.LLM_VISION_REASONING_EFFORT = ""
    llm.LLM_REASONING_EFFORT = ""
    vision.LLM_VISION_REASONING_EFFORT = ""

    assert env_config.apply_env_to_runtime("LLM_REASONING_EFFORT", "low") is False
    assert env_config.apply_env_to_runtime("LLM_VISION_REASONING_EFFORT", "none") is False

    assert config.LLM_REASONING_EFFORT == "low"
    assert llm.LLM_REASONING_EFFORT == "low"
    assert config.LLM_VISION_REASONING_EFFORT == "none"
    assert vision.LLM_VISION_REASONING_EFFORT == "none"


def test_apply_env_to_runtime_updates_default_llm_base_urls(monkeypatch):
    import config
    import bot.agent as agent
    import bot.llm as llm
    import bot.vision as vision

    old_base_url = config.LLM_BASE_URL
    monkeypatch.setattr(config, "LLM_BASE_URL", old_base_url)
    monkeypatch.setattr(config, "LLM_PONDER_BASE_URL", old_base_url)
    monkeypatch.setattr(config, "LLM_VISION_BASE_URL", old_base_url)
    monkeypatch.setattr(llm.client, "base_url", llm.client.base_url)
    monkeypatch.setattr(agent.client, "base_url", agent.client.base_url)
    monkeypatch.setattr(vision.client, "base_url", vision.client.base_url)

    restart_required = env_config.apply_env_to_runtime(
        "LLM_BASE_URL", "https://gateway.example.test/v1"
    )

    assert restart_required is False
    assert config.LLM_BASE_URL == "https://gateway.example.test/v1"
    assert config.LLM_PONDER_BASE_URL == "https://gateway.example.test/v1"
    assert config.LLM_VISION_BASE_URL == "https://gateway.example.test/v1"
    assert str(llm.client.base_url) == "https://gateway.example.test/v1/"
    assert str(agent.client.base_url) == "https://gateway.example.test/v1/"
    assert str(vision.client.base_url) == "https://gateway.example.test/v1/"


def test_set_env_value_updates_prompt_cache_runtime(env_file, monkeypatch):
    env_file.write_text("LLM_PROMPT_CACHE=true\n", encoding="utf-8")
    monkeypatch.setenv("LLM_PROMPT_CACHE", "true")
    import config
    import bot.llm as llm

    config.LLM_PROMPT_CACHE = True
    llm.LLM_PROMPT_CACHE = True

    restart_required, message = env_config.set_env_value("LLM_PROMPT_CACHE", "false")

    assert restart_required is False
    assert "Updated LLM_PROMPT_CACHE" in message
    assert "LLM_PROMPT_CACHE=false" in env_file.read_text(encoding="utf-8")
    assert os.environ["LLM_PROMPT_CACHE"] == "false"
    assert config.LLM_PROMPT_CACHE is False
    assert llm.LLM_PROMPT_CACHE is False


def test_format_env_panel_lists_prompt_cache(env_file):
    env_file.write_text("LLM_PROMPT_CACHE=true\n", encoding="utf-8")

    panel = env_config.format_env_panel()

    assert "LLM_PROMPT_CACHE=true" in panel



def test_format_env_panel_lists_masked_values(env_file):
    env_file.write_text(
        "LLM_API_KEY=supersecretvalue\nLLM_MODEL=google/gemini\n",
        encoding="utf-8",
    )

    panel = env_config.format_env_panel()

    assert str(env_file) in panel
    assert "LLM_MODEL=google/gemini" in panel
    assert "super" not in panel



def test_set_env_value_accepts_firecrawl_keys(env_file):
    env_file.write_text("", encoding="utf-8")

    restart_required, message = env_config.set_env_value("FIRECRAWL_API_KEY", "fc-test-key")

    assert restart_required is False
    assert "Updated FIRECRAWL_API_KEY" in message
    assert "FIRECRAWL_API_KEY=fc-test-key" in env_file.read_text(encoding="utf-8")


def test_apply_env_to_runtime_updates_firecrawl(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "")
    import config
    import bot.agent as agent

    config.FIRECRAWL_API_KEY = None
    agent.FIRECRAWL_API_KEY = None

    restart_required = env_config.apply_env_to_runtime("FIRECRAWL_API_KEY", "fc-new-key")

    assert restart_required is False
    assert config.FIRECRAWL_API_KEY == "fc-new-key"
    assert agent.FIRECRAWL_API_KEY == "fc-new-key"


def test_format_env_panel_masks_firecrawl_api_key(env_file):
    env_file.write_text("FIRECRAWL_API_KEY=fc-supersecretkey\n", encoding="utf-8")

    panel = env_config.format_env_panel()

    assert "FIRECRAWL_API_KEY=" in panel
    assert "supersecretkey" not in panel
