import json

import pytest

from bot import build_info, system


@pytest.fixture
def build_info_file(tmp_path, monkeypatch):
    path = tmp_path / ".build-info.json"
    monkeypatch.setattr(build_info, "BUILD_INFO_PATH", path)
    return path


def test_load_build_info_reads_baked_file(build_info_file):
    build_info_file.write_text(
        json.dumps(
            {
                "commit": "abc1234567890",
                "short": "abc1234",
                "subject": "feat: version",
                "date": "2026-06-26 12:00:00 +0000",
            }
        ),
        encoding="utf-8",
    )

    info = build_info.load_build_info()

    assert info is not None
    assert info["short"] == "abc1234"
    assert info["subject"] == "feat: version"


def test_load_build_info_falls_back_to_env(monkeypatch):
    monkeypatch.setattr(build_info, "BUILD_INFO_PATH", build_info.BUILD_INFO_PATH.with_name("missing.json"))
    monkeypatch.setenv("GIT_COMMIT", "deadbeefdeadbeef")
    monkeypatch.setenv("GIT_COMMIT_SHORT", "deadbee")
    monkeypatch.setenv("GIT_COMMIT_SUBJECT", "from env")

    info = build_info.load_build_info()

    assert info is not None
    assert info["commit"] == "deadbeefdeadbeef"
    assert info["subject"] == "from env"


def test_format_build_info():
    text = build_info.format_build_info(
        {
            "commit": "abc1234567890",
            "short": "abc1234",
            "subject": "feat: version",
            "date": "2026-06-26 12:00:00 +0000",
        }
    )

    assert "Commit: abc1234 (abc1234567890)" in text
    assert "feat: version" in text


@pytest.mark.asyncio
async def test_get_version_info_uses_baked_metadata(monkeypatch):
    monkeypatch.setattr(
        system,
        "load_build_info",
        lambda: {
            "commit": "abc1234567890",
            "short": "abc1234",
            "subject": "feat: version",
            "date": "2026-06-26 12:00:00 +0000",
        },
    )

    info = await system.get_version_info()

    assert "abc1234" in info
    assert "feat: version" in info
