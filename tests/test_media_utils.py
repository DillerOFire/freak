import pytest
from unittest.mock import MagicMock, patch
from bot import media_utils


@pytest.fixture
def mock_ytdlp():
    with patch("yt_dlp.YoutubeDL") as mock:
        yield mock


@pytest.fixture
def mock_cv2():
    with patch("cv2.VideoCapture") as mock_cap, patch("cv2.imencode") as mock_imencode:
        yield mock_cap, mock_imencode


def test_download_video_ytdlp_success(mock_ytdlp):
    """Test successful video download."""
    # Setup mock
    instance = mock_ytdlp.return_value.__enter__.return_value
    instance.download.return_value = (
        None  # download returns None on success logic in code
    )

    # Mock glob to find the file
    with (
        patch("glob.glob") as mock_glob,
        patch("tempfile.gettempdir", return_value="/tmp"),
        patch("uuid.uuid4", return_value="test_uuid"),
    ):
        mock_glob.return_value = ["/tmp/test_uuid.mp4"]

        result = media_utils.download_video_ytdlp("https://example.com/video")

        assert result == "/tmp/test_uuid.mp4"
        instance.download.assert_called_once_with(["https://example.com/video"])

        # Verify options
        args, _ = mock_ytdlp.call_args
        opts = args[0]
        assert opts["max_filesize"] == 50 * 1024 * 1024
        assert opts["noplaylist"] is True


def test_download_video_ytdlp_failure(mock_ytdlp):
    """Test video download failure."""
    instance = mock_ytdlp.return_value.__enter__.return_value
    instance.download.side_effect = Exception("Download failed")

    result = media_utils.download_video_ytdlp("https://example.com/video")
    assert result is None


def test_download_audio_ytdlp_success(mock_ytdlp):
    """Test successful audio download with metadata."""
    instance = mock_ytdlp.return_value.__enter__.return_value

    # Mock extract_info return
    instance.extract_info.return_value = {
        "title": "Test Song",
        "description": "A test song",
        "duration": 120,
        "uploader": "Test Artist",
    }

    with (
        patch("os.path.exists") as mock_exists,
        patch("tempfile.gettempdir", return_value="/tmp"),
        patch("uuid.uuid4", return_value="test_uuid"),
    ):
        # Mock that files exist
        def exists_side_effect(path):
            if path.endswith(".mp3"):
                return True
            if path.endswith(".jpg"):
                return True
            return False

        mock_exists.side_effect = exists_side_effect

        result = media_utils.download_audio_ytdlp("https://example.com/audio")

        assert result is not None
        assert result["title"] == "Test Song"
        assert result["audio_path"].endswith(".mp3")
        assert result["thumbnail_path"].endswith(".jpg")


def test_extract_frames_from_video(mock_cv2):
    """Test frame extraction."""
    mock_cap_cls, mock_imencode = mock_cv2
    mock_cap = mock_cap_cls.return_value

    # Mock video properties
    mock_cap.isOpened.return_value = True
    mock_cap.get.return_value = 100  # 100 frames total

    # Mock reading frames (return True, frame_data)
    mock_cap.read.return_value = (True, "frame_data")

    # Mock encoding
    # imencode returns (retval, buffer), where buffer is a numpy array (or similar) that has tobytes()
    mock_buffer = MagicMock()
    mock_buffer.tobytes.return_value = b"encoded_image"
    mock_imencode.return_value = (True, mock_buffer)

    frames = media_utils.extract_frames_from_video("video.mp4", max_frames=5)

    assert len(frames) == 5
    assert all(f == b"encoded_image" for f in frames)
    mock_cap.release.assert_called_once()
