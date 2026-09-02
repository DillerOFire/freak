from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


SCRIPT = Path(__file__).parents[1] / "deployment" / "freak_release_updater.py"
SPEC = importlib.util.spec_from_file_location("freak_release_updater", SCRIPT)
assert SPEC and SPEC.loader
UPDATER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = UPDATER
SPEC.loader.exec_module(UPDATER)


def test_latest_release_requires_a_matching_digest() -> None:
    digest = "a" * 64
    releases = [{"tag_name": "continuous-" + "b" * 40, "draft": False, "prerelease": False, "assets": [{"name": "image.env", "browser_download_url": "image"}, {"name": "image.env.release-id", "browser_download_url": "id"}]}]
    with (
        patch.object(UPDATER, "request_json", return_value=releases),
        patch.object(UPDATER, "download_text", side_effect=[f"FREAK_IMAGE=ghcr.io/dillerofire/freak@sha256:{digest}", digest]),
    ):
        assert UPDATER.latest_release() == ("continuous-" + "b" * 40, f"ghcr.io/dillerofire/freak@sha256:{digest}")


def test_latest_release_rejects_mismatched_release_identifier() -> None:
    digest = "a" * 64
    releases = [{"tag_name": "continuous-" + "b" * 40, "draft": False, "prerelease": False, "assets": [{"name": "image.env", "browser_download_url": "image"}, {"name": "image.env.release-id", "browser_download_url": "id"}]}]
    with (
        patch.object(UPDATER, "request_json", return_value=releases),
        patch.object(UPDATER, "download_text", side_effect=[f"FREAK_IMAGE=ghcr.io/dillerofire/freak@sha256:{digest}", "c" * 64]),
        pytest.raises(ValueError, match="does not match"),
    ):
        UPDATER.latest_release()


def test_deploy_rolls_both_bots_back_when_health_check_fails(tmp_path: Path) -> None:
    old_images = iter(["ghcr.io/dillerofire/freak@sha256:" + "a" * 64, "ghcr.io/dillerofire/freak@sha256:" + "b" * 64])
    image = "ghcr.io/dillerofire/freak@sha256:" + "c" * 64
    with (
        patch.object(UPDATER, "current_image", side_effect=lambda _: next(old_images)),
        patch.object(UPDATER, "run"),
        patch.object(UPDATER, "apply"),
        patch.object(UPDATER, "wait_healthy", side_effect=RuntimeError("unhealthy")),
        pytest.raises(RuntimeError, match="unhealthy"),
    ):
        UPDATER.deploy(image, tmp_path, 1)

    assert image not in (tmp_path / "freak.compose.yml").read_text(encoding="utf-8")
    assert image not in (tmp_path / "personfreak.compose.yml").read_text(encoding="utf-8")


def test_main_waits_cleanly_before_the_first_verified_release() -> None:
    with (
        patch.object(UPDATER, "latest_release", side_effect=UPDATER.NoVerifiedReleaseError("not ready")),
        patch.object(sys, "argv", [str(SCRIPT)]),
    ):
        assert UPDATER.main() == 0
