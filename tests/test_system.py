import pytest
from unittest.mock import MagicMock, patch
from bot import system


@pytest.fixture
def mock_subprocess():
    with patch("subprocess.run") as mock:
        yield mock


@pytest.mark.asyncio
async def test_update_ytdlp_success(mock_subprocess):
    """Test successful yt-dlp update."""
    mock_subprocess.return_value.returncode = 0
    mock_subprocess.return_value.stdout = "Successfully Installed yt-dlp"

    success, msg = await system.update_ytdlp_package()

    assert success is True
    assert "updated successfully" in msg
    mock_subprocess.assert_called_once()
    args, _ = mock_subprocess.call_args
    assert "uv" in args[0]
    assert any("yt-dlp" in arg for arg in args[0])


@pytest.mark.asyncio
async def test_check_for_updates_available(mock_subprocess):
    """Test when updates are available."""
    # First call is git fetch, second is git status
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
    mock_subprocess.assert_called_once_with(
        ["git", "pull"], capture_output=True, text=True, check=False
    )


def test_restart_bot():
    """Test bot restart (sys.exit)."""
    with pytest.raises(SystemExit) as pytest_wrapped_e:
        system.restart_bot()
    assert pytest_wrapped_e.type is SystemExit
    assert pytest_wrapped_e.value.code == 0
