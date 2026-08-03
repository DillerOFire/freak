import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from bot import media_utils
from bot.media_utils import YtDlpResult


@pytest.fixture
def mock_ytdlp():
    with patch("yt_dlp.YoutubeDL") as mock:
        yield mock


@pytest.fixture
def mock_cv2():
    with patch("cv2.VideoCapture") as mock_cap, patch("cv2.imencode") as mock_imencode:
        yield mock_cap, mock_imencode


@pytest.fixture(autouse=True)
def clear_cookie_notify_cooldown():
    media_utils._last_cookie_notify_at.clear()
    yield
    media_utils._last_cookie_notify_at.clear()


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

        assert result.ok
        assert result.path == "/tmp/test_uuid.mp4"
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
    assert not result.ok
    assert result.path is None
    assert "Download failed" in (result.error or "")
    assert result.cookie_issue is False


def test_download_video_ytdlp_cookie_403(mock_ytdlp, tmp_path):
    """403 Forbidden is treated as a cookie/auth issue when cookies were expected."""
    instance = mock_ytdlp.return_value.__enter__.return_value
    instance.download.side_effect = Exception(
        "ERROR: unable to download video data: HTTP Error 403: Forbidden"
    )
    cookies = tmp_path / "youtube.txt"
    cookies.write_text("# Netscape\n", encoding="utf-8")

    result = media_utils.download_video_ytdlp(
        "https://youtube.com/watch?v=abc", str(cookies)
    )
    assert not result.ok
    assert result.cookie_issue is True
    assert result.cookies_present is True


def test_download_video_ytdlp_missing_cookies_is_cookie_issue(mock_ytdlp, tmp_path):
    """Missing expected cookies file is a cookie issue on failure."""
    instance = mock_ytdlp.return_value.__enter__.return_value
    instance.download.side_effect = Exception("network boom")
    missing = str(tmp_path / "youtube.txt")

    result = media_utils.download_video_ytdlp(
        "https://youtube.com/watch?v=abc", missing
    )
    assert not result.ok
    assert result.cookie_issue is True
    assert result.cookies_present is False


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

        assert result.ok
        assert result.info is not None
        assert result.info["title"] == "Test Song"
        assert result.info["audio_path"].endswith(".mp3")
        assert result.info["thumbnail_path"].endswith(".jpg")


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


def test_detect_cookie_issue_markers():
    assert media_utils._detect_cookie_issue(
        "ERROR: Sign in to confirm your age", None, False
    )
    assert media_utils._detect_cookie_issue(
        "HTTP Error 403: Forbidden", "/data/cookies/youtube.txt", True
    )
    assert not media_utils._detect_cookie_issue(
        "Unsupported URL", None, False
    )


def test_normalize_netscape_cookies_converts_spaces_to_tabs():
    raw = (
        "# Netscape HTTP Cookie File\n"
        ".youtube.com  TRUE  /  TRUE  1820105380  SID  secretvalue\n"
        ".youtube.com\tTRUE\t/\tFALSE\t0\tPREF\thl=en\n"
    )
    text, names = media_utils.normalize_netscape_cookies(raw)
    assert names == ["SID", "PREF"]
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert all("\t" in ln for ln in lines)
    assert " secretvalue" not in lines[0]  # tabs, not multi-spaces as separators
    assert lines[0].split("\t")[5] == "SID"
    assert lines[0].split("\t")[6] == "secretvalue"


def test_normalize_netscape_cookies_preserves_httponly_rows():
    text, names = media_utils.normalize_netscape_cookies(
        "#HttpOnly_.youtube.com TRUE / TRUE 1820105380 SID secretvalue\n"
    )

    assert names == ["SID"]
    assert "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1820105380\tSID\tsecretvalue" in text


def test_save_netscape_cookies_rejects_empty(tmp_path):
    path = tmp_path / "youtube.txt"
    with pytest.raises(ValueError, match="No valid Netscape"):
        media_utils.save_netscape_cookies(str(path), "# only a comment\n")


def test_save_netscape_cookies_reports_session(tmp_path):
    path = tmp_path / "youtube.txt"
    raw = (
        ".youtube.com TRUE / TRUE 1820105380 SID abc\n"
        ".youtube.com TRUE / TRUE 1820105380 LOGIN_INFO def\n"
        ".youtube.com TRUE / TRUE 0 VISITOR_INFO1_LIVE xyz\n"
    )
    count, names, session = media_utils.save_netscape_cookies(str(path), raw)
    assert count == 3
    assert "SID" in names and "LOGIN_INFO" in names
    assert "SID" in session and "LOGIN_INFO" in session
    written = path.read_text(encoding="utf-8")
    assert "\tSID\t" in written


def test_download_video_uses_cookie_copy_not_source(mock_ytdlp, tmp_path):
    """yt-dlp must receive a temp cookiefile so it cannot wipe the stored jar."""
    instance = mock_ytdlp.return_value.__enter__.return_value
    instance.download.return_value = None
    cookies = tmp_path / "youtube.txt"
    cookies.write_text(
        ".youtube.com\tTRUE\t/\tTRUE\t1820105380\tSID\tabc\n", encoding="utf-8"
    )
    original = cookies.read_text(encoding="utf-8")

    with (
        patch("glob.glob", return_value=["/tmp/out.mp4"]),
        patch("tempfile.gettempdir", return_value="/tmp"),
        patch("uuid.uuid4", return_value="test_uuid"),
    ):
        result = media_utils.download_video_ytdlp(
            "https://youtube.com/watch?v=abc", str(cookies)
        )

    assert result.ok
    opts = mock_ytdlp.call_args[0][0]
    assert opts["cookiefile"] != str(cookies)
    assert opts["cookiefile"].endswith(".txt")
    assert "js_runtimes" in opts
    assert "remote_components" in opts
    # Stored jar unchanged
    assert cookies.read_text(encoding="utf-8") == original


def test_cookie_refresh_instructions_include_service_links_and_tools():
    text = media_utils.cookie_refresh_instructions("youtube")
    assert "https://www.youtube.com" in text
    assert "Get cookies.txt LOCALLY" in text
    assert "chromewebstore.google.com" in text
    assert "addons.mozilla.org" in text
    assert "/update_cookies youtube" in text
    assert "yt-dlp/wiki/FAQ" in text


def test_format_cookie_failure_admin_message_is_actionable():
    result = YtDlpResult(
        error="HTTP Error 403: Forbidden",
        cookie_issue=True,
        cookies_path="/data/cookies/youtube.txt",
        cookies_present=True,
    )
    text = media_utils.format_cookie_failure_admin_message(
        url="https://www.youtube.com/watch?v=RqPUrRdCgRU",
        result=result,
        service="youtube",
    )
    assert "Cookie / auth failure — YouTube" in text
    assert "RqPUrRdCgRU" in text
    assert "invalid/expired" in text
    assert "https://www.youtube.com" in text
    assert "https://accounts.google.com" in text
    assert "/update_cookies youtube" in text
    assert len(text) <= 4000


@pytest.mark.asyncio
async def test_notify_admin_cookie_failure_sends_and_rate_limits():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    result = YtDlpResult(
        error="HTTP Error 403: Forbidden",
        cookie_issue=True,
        cookies_path="/data/cookies/youtube.txt",
        cookies_present=True,
    )

    with patch.object(media_utils, "ADMIN_ID", 42):
        sent = await media_utils.notify_admin_cookie_failure(
            bot, url="https://youtu.be/x", result=result, service="youtube"
        )
        assert sent is True
        bot.send_message.assert_called_once()
        kwargs = bot.send_message.call_args.kwargs
        text = kwargs["text"]
        assert kwargs["chat_id"] == 42
        assert kwargs.get("disable_web_page_preview") is True
        assert "YouTube" in text
        assert "https://www.youtube.com" in text
        assert "Get cookies.txt LOCALLY" in text
        assert "/update_cookies youtube" in text

        # Second notify within cooldown is skipped
        sent2 = await media_utils.notify_admin_cookie_failure(
            bot, url="https://youtu.be/y", result=result, service="youtube"
        )
        assert sent2 is False
        assert bot.send_message.call_count == 1


@pytest.mark.asyncio
async def test_notify_admin_cookie_failure_adds_preselected_web_cookie_button():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    result = YtDlpResult(
        error="HTTP Error 403: Forbidden",
        cookie_issue=True,
        cookies_path="/data/cookies/youtube.txt",
        cookies_present=True,
    )

    with patch.object(media_utils, "ADMIN_ID", 42), patch.object(
        media_utils, "WEB_SETTINGS_URL", "https://bot.example.test"
    ):
        sent = await media_utils.notify_admin_cookie_failure(
            bot, url="https://youtu.be/x", result=result, service="youtube"
        )

    assert sent is True
    button = bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Refresh cookies in web app"
    assert button.web_app.url == "https://bot.example.test/cookies?service=youtube"


@pytest.mark.asyncio
async def test_notify_admin_skips_non_cookie_failures():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    result = YtDlpResult(error="file too large", cookie_issue=False)

    with patch.object(media_utils, "ADMIN_ID", 42):
        sent = await media_utils.notify_admin_cookie_failure(
            bot, url="https://youtu.be/x", result=result
        )
    assert sent is False
    bot.send_message.assert_not_called()
