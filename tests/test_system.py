import os
import pytest
from unittest.mock import MagicMock, patch
from bot import system


@pytest.fixture(autouse=True)
def reset_uv_cache():
    system.resolve_uv_executable.cache_clear()
    yield
    system.resolve_uv_executable.cache_clear()


@pytest.fixture
def mock_uv_path():
    with patch("bot.system.resolve_uv_executable", return_value="/usr/bin/uv"):
        yield "/usr/bin/uv"


@pytest.fixture
def mock_subprocess():
    with patch("bot.system._run_cmd") as mock:
        yield mock


@pytest.mark.asyncio
async def test_update_ytdlp_stages_verifies_and_activates(
    mock_subprocess, mock_uv_path, tmp_path, monkeypatch
):
    target_dir = str(tmp_path / "python-packages")
    base_target = tmp_path / "base-site-packages"
    (base_target / "yt_dlp").mkdir(parents=True)
    (base_target / "yt_dlp" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr("config.YTDLP_PACKAGE_DIR", target_dir)
    monkeypatch.setattr(system, "_active_ytdlp_version", lambda: "2025.01.01")
    monkeypatch.setattr(system, "_active_ytdlp_target", lambda: str(base_target))
    def run_command(args, **_kwargs):
        if "--target" in args:
            staging = args[args.index("--target") + 1]
            os.makedirs(os.path.join(staging, "yt_dlp"), exist_ok=True)
            open(os.path.join(staging, "yt_dlp", "__init__.py"), "w").close()
            return MagicMock(returncode=0, stdout="Successfully Installed yt-dlp", stderr="")
        return MagicMock(returncode=0, stdout="2026.08.03.1\n", stderr="")

    mock_subprocess.side_effect = run_command

    success, msg = await system.update_ytdlp_package()

    assert success is True
    assert "updated successfully" in msg
    assert mock_subprocess.call_count == 2
    install_args = mock_subprocess.call_args_list[0].args[0]
    assert install_args[0] == mock_uv_path
    assert "--target" in install_args
    assert system.YTDLP_GIT_SOURCE in install_args
    current = tmp_path / "python-packages" / "current"
    assert current.is_symlink()
    assert (current / "yt_dlp" / "__init__.py").is_file()
    assert (tmp_path / "python-packages" / "previous").resolve() == base_target


@pytest.mark.asyncio
async def test_update_ytdlp_failed_stage_does_not_replace_current(
    mock_subprocess, mock_uv_path, tmp_path, monkeypatch
):
    target_dir = str(tmp_path / "python-packages")
    monkeypatch.setattr("config.YTDLP_PACKAGE_DIR", target_dir)
    old_release = tmp_path / "python-packages" / "releases" / "old"
    (old_release / "yt_dlp").mkdir(parents=True)
    (tmp_path / "python-packages" / "current").symlink_to(old_release)
    mock_subprocess.return_value = MagicMock(
        returncode=1, stdout="", stderr="network failure"
    )

    success, msg = await system.update_ytdlp_package()

    assert success is False
    assert "network failure" in msg
    assert (tmp_path / "python-packages" / "current").resolve() == old_release
    assert not list((tmp_path / "python-packages" / "releases").glob(".staging-*"))


@pytest.mark.asyncio
async def test_rollback_ytdlp_swaps_current_and_previous(
    mock_subprocess, tmp_path, monkeypatch
):
    package_root = tmp_path / "python-packages"
    current_release = package_root / "releases" / "current-build"
    previous_release = package_root / "releases" / "previous-build"
    for release in (current_release, previous_release):
        (release / "yt_dlp").mkdir(parents=True)
        (release / "yt_dlp" / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "current").symlink_to(current_release)
    (package_root / "previous").symlink_to(previous_release)
    monkeypatch.setattr("config.YTDLP_PACKAGE_DIR", str(package_root))
    mock_subprocess.return_value = MagicMock(
        returncode=0, stdout="2026.07.31.1\n", stderr=""
    )

    success, message = await system.rollback_ytdlp_package()

    assert success is True
    assert "2026.07.31.1" in message
    assert (package_root / "current").resolve() == previous_release
    assert (package_root / "previous").resolve() == current_release


@pytest.mark.asyncio
async def test_check_for_updates_available(mock_subprocess):
    """Test when updates are available."""
    mock_subprocess.side_effect = [
        MagicMock(returncode=0),  # git fetch
        MagicMock(returncode=0, stdout="Your branch is behind"),  # git status
    ]

    available = await system.check_for_updates()
    assert available is True
    assert mock_subprocess.call_count == 2


@pytest.mark.asyncio
async def test_pull_updates_success(mock_subprocess):
    """Test successful git pull."""
    mock_subprocess.return_value.returncode = 0
    mock_subprocess.return_value.stdout = "Updating 123..456"

    success, msg = await system.pull_updates()
    assert success is True
    assert "Successfully pulled" in msg
    mock_subprocess.assert_called_once_with(["git", "pull"], check=False)


@pytest.mark.asyncio
async def test_sync_dependencies_success(mock_subprocess, mock_uv_path):
    mock_subprocess.return_value.returncode = 0
    mock_subprocess.return_value.stdout = "Resolved 43 packages"

    success, msg = await system.sync_dependencies()

    assert success is True
    assert "Dependencies synced" in msg
    mock_subprocess.assert_called_once_with([mock_uv_path, "sync"], check=False)


@pytest.mark.asyncio
async def test_verify_startup_success(mock_subprocess, mock_uv_path):
    mock_subprocess.return_value.returncode = 0

    success, msg = await system.verify_startup()

    assert success is True
    assert "Startup verification passed" in msg
    args, _ = mock_subprocess.call_args
    assert args[0][:3] == [mock_uv_path, "run", "python"]


@pytest.mark.asyncio
async def test_apply_bot_updates_success(mock_subprocess):
    mock_subprocess.side_effect = [
        MagicMock(returncode=0, stdout="abc123\n"),  # rev-parse
        MagicMock(returncode=0, stdout="Updating 1..2"),  # git pull
        MagicMock(returncode=0, stdout="Installed aiohttp"),  # uv sync
        MagicMock(returncode=0, stdout=""),  # verify startup
    ]

    with patch("bot.system.resolve_uv_executable", return_value="/usr/bin/uv"):
        success, msg = await system.apply_bot_updates()

    assert success is True
    assert "Successfully pulled" in msg
    assert "Dependencies synced" in msg
    assert "Startup verification passed" in msg


@pytest.mark.asyncio
async def test_apply_bot_updates_rolls_back_on_sync_failure(mock_subprocess):
    mock_subprocess.side_effect = [
        MagicMock(returncode=0, stdout="abc123\n"),  # rev-parse
        MagicMock(returncode=0, stdout="Updating 1..2"),  # git pull
        MagicMock(returncode=1, stderr="uv sync failed"),  # uv sync
        MagicMock(returncode=0, stdout=""),  # git reset --hard
        MagicMock(returncode=0, stdout=""),  # uv sync after rollback
    ]

    with patch("bot.system.resolve_uv_executable", return_value="/usr/bin/uv"):
        success, msg = await system.apply_bot_updates()

    assert success is False
    assert "Dependency sync failed" in msg
    assert "Rolled back" in msg
    assert mock_subprocess.call_count == 5


@pytest.mark.asyncio
async def test_apply_bot_updates_rolls_back_on_verify_failure(mock_subprocess):
    mock_subprocess.side_effect = [
        MagicMock(returncode=0, stdout="abc123\n"),  # rev-parse
        MagicMock(returncode=0, stdout="Updating 1..2"),  # git pull
        MagicMock(returncode=0, stdout=""),  # uv sync
        MagicMock(returncode=1, stderr="ModuleNotFoundError: aiohttp"),  # verify
        MagicMock(returncode=0, stdout=""),  # git reset --hard
        MagicMock(returncode=0, stdout=""),  # uv sync after rollback
    ]

    with patch("bot.system.resolve_uv_executable", return_value="/usr/bin/uv"):
        success, msg = await system.apply_bot_updates()

    assert success is False
    assert "Startup verification failed" in msg
    assert "Rolled back" in msg


def test_resolve_uv_executable_prefers_env_override(tmp_path):
    uv_path = tmp_path / "custom-uv"
    uv_path.write_text("#!/bin/sh\n")
    uv_path.chmod(0o755)

    with patch.dict(os.environ, {"UV_EXECUTABLE": str(uv_path)}):
        assert system.resolve_uv_executable() == str(uv_path)


def test_resolve_uv_executable_falls_back_to_local_bin(tmp_path, monkeypatch):
    monkeypatch.delenv("UV_EXECUTABLE", raising=False)
    local_uv = tmp_path / ".local" / "bin" / "uv"
    local_uv.parent.mkdir(parents=True)
    local_uv.write_text("#!/bin/sh\n")
    local_uv.chmod(0o755)

    with (
        patch("bot.system.shutil.which", return_value=None),
        patch("bot.system.os.path.expanduser", return_value=str(tmp_path)),
    ):
        assert system.resolve_uv_executable() == str(local_uv)


def test_resolve_uv_executable_falls_back_to_cargo_bin(tmp_path, monkeypatch):
    monkeypatch.delenv("UV_EXECUTABLE", raising=False)
    cargo_uv = tmp_path / ".cargo" / "bin" / "uv"
    cargo_uv.parent.mkdir(parents=True)
    cargo_uv.write_text("#!/bin/sh\n")
    cargo_uv.chmod(0o755)

    with (
        patch("bot.system.shutil.which", return_value=None),
        patch("bot.system.os.path.expanduser", return_value=str(tmp_path)),
    ):
        assert system.resolve_uv_executable() == str(cargo_uv)


def test_resolve_uv_executable_raises_when_missing(monkeypatch):
    monkeypatch.delenv("UV_EXECUTABLE", raising=False)

    with (
        patch("bot.system.shutil.which", return_value=None),
        patch("bot.system.os.path.isfile", return_value=False),
    ):
        with pytest.raises(FileNotFoundError, match="uv executable not found"):
            system.resolve_uv_executable()


@pytest.mark.asyncio
async def test_get_version_info():
    with (
        patch("bot.system.load_build_info", return_value=None),
        patch("bot.system._run_cmd") as mock_run,
    ):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                "fullhash1234567890abcdef\n"
                "abc1234\n"
                "feat: add version command\n"
                "2026-06-26 12:00:00 +0000\n"
            ),
        )
        info = await system.get_version_info()

    assert "abc1234" in info
    assert "feat: add version command" in info


def test_restart_bot():
    """Test bot restart (sys.exit)."""
    with pytest.raises(SystemExit) as pytest_wrapped_e:
        system.restart_bot()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 0
