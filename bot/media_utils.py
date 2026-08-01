import cv2
import tempfile
import logging
import os
import time
import yt_dlp
import glob
import uuid
from dataclasses import dataclass
from telegram import File

from config import ADMIN_ID

# Rate-limit cookie failure DMs per service (seconds).
_COOKIE_NOTIFY_COOLDOWN_SEC = 15 * 60
_last_cookie_notify_at: dict[str, float] = {}

# Substrings in yt-dlp errors that usually mean cookies/auth need refresh.
_COOKIE_FAILURE_MARKERS = (
    "sign in",
    "login required",
    "please log in",
    "cookies",
    "cookie",
    "http error 401",
    "http error 403",
    "403: forbidden",
    "401: unauthorized",
    "confirm your age",
    "age-restricted",
    "age restricted",
    "this video is private",
    "private video",
    "members-only",
    "members only",
    "join this channel",
    "not a bot",
    "bot check",
    "authentication",
    "unable to download video data",
    "requested format is not available",
)


@dataclass
class YtDlpResult:
    """Outcome of a yt-dlp video/audio download attempt."""

    path: str | None = None
    info: dict | None = None
    error: str | None = None
    cookie_issue: bool = False
    cookies_path: str | None = None
    cookies_present: bool = False

    @property
    def ok(self) -> bool:
        return self.path is not None or self.info is not None


def _cookies_present(cookies_path: str | None) -> bool:
    return bool(cookies_path and os.path.exists(cookies_path))


def _detect_cookie_issue(
    error: str | None, cookies_path: str | None, cookies_present: bool
) -> bool:
    """True when failure looks like missing/invalid/expired cookies."""
    if not error:
        return False
    err = error.lower()
    marker_hit = any(marker in err for marker in _COOKIE_FAILURE_MARKERS)
    # Service expected cookies but file is missing, and download failed.
    if cookies_path and not cookies_present:
        return True
    return marker_hit


def service_name_from_cookies_path(cookies_path: str | None) -> str:
    if not cookies_path:
        return "unknown"
    base = os.path.basename(cookies_path)
    if base.endswith(".txt"):
        return base[: -len(".txt")] or "unknown"
    return base or "unknown"


# Cookie names that usually mean a logged-in browser session (not just visitor IDs).
_SESSION_COOKIE_NAMES = frozenset(
    {
        "sid",
        "hsid",
        "ssid",
        "apisid",
        "sapisid",
        "login_info",
        "__secure-1psid",
        "__secure-3psid",
        "sessionid",
        "auth_token",
        "auth_multi",
        "twid",
        "li_at",
        "csrftoken",
    }
)


def normalize_netscape_cookies(content: str) -> tuple[str, list[str]]:
    """
    Normalize a Netscape cookies.txt body to tab-separated rows.

    Many browser extensions export space-separated fields. yt-dlp requires
    tabs and skips space-separated rows with "invalid length 1".
    Returns (normalized_text, list_of_cookie_names).
    """
    out_lines: list[str] = []
    names: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip("\r")
        if not line.strip():
            out_lines.append("")
            continue
        if line.lstrip().startswith("#"):
            out_lines.append(line.rstrip())
            continue

        if "\t" in line:
            fields = line.split("\t")
        else:
            # Space-separated export: first 6 fields are fixed; value is the rest.
            fields = line.split()
            if len(fields) > 7:
                fields = fields[:6] + [" ".join(fields[6:])]

        if len(fields) < 7:
            logging.warning(
                "Skipping cookie line with %d fields (need 7): %s…",
                len(fields),
                line[:80],
            )
            continue

        domain, flag, path, secure, expires, name, value = fields[:7]
        # Normalize boolean flags to TRUE/FALSE as Netscape expects.
        flag = "TRUE" if flag.upper() == "TRUE" else "FALSE"
        secure = "TRUE" if secure.upper() == "TRUE" else "FALSE"
        out_lines.append(
            "\t".join([domain, flag, path, secure, expires, name, value])
        )
        names.append(name)

    text = "\n".join(out_lines)
    if text and not text.endswith("\n"):
        text += "\n"
    if not text.startswith("#"):
        text = "# Netscape HTTP Cookie File\n" + text
    return text, names


def save_netscape_cookies(path: str, content: str) -> tuple[int, list[str], list[str]]:
    """
    Normalize and write cookies.txt. Returns (count, all_names, session_names).
    Raises ValueError if no valid cookie rows remain after normalization.
    """
    normalized, names = normalize_netscape_cookies(content)
    if not names:
        raise ValueError(
            "No valid Netscape cookie rows found. Export cookies.txt with "
            "TAB-separated fields (e.g. “Get cookies.txt LOCALLY”)."
        )
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(normalized)
    session = [n for n in names if n.lower() in _SESSION_COOKIE_NAMES]
    return len(names), names, session


def _prepare_cookiefile_copy(cookies_path: str) -> tuple[str, int, list[str]]:
    """
    Build a temp Netscape cookiefile for yt-dlp.

    Uses a copy so yt-dlp cannot rewrite/empty the stored jar on download.
    Normalizes space-separated exports to tabs.
    """
    with open(cookies_path, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    normalized, names = normalize_netscape_cookies(raw)
    fd, tmp_path = tempfile.mkstemp(prefix="ytdlp-cookies-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(normalized)
    return tmp_path, len(names), names


def _base_ydl_opts() -> dict:
    """Shared yt-dlp options including JS runtime for YouTube EJS challenges."""
    return {
        "quiet": True,
        "noplaylist": True,
        # Deno is installed in the Docker image; node listed as optional fallback.
        "js_runtimes": {"deno": {}, "node": {}},
        # Allow fetching challenge solver scripts when the package needs them.
        "remote_components": {"ejs:github"},
        "logger": YtDlpLogger(),
    }


# Per-service guidance for refreshing Netscape cookies.txt used by yt-dlp.
# Keep links stable; prefer official/extension store pages and yt-dlp wiki.
_SERVICE_COOKIE_GUIDES: dict[str, dict] = {
    "youtube": {
        "label": "YouTube",
        "sites": [
            "https://www.youtube.com",
            "https://accounts.google.com",
        ],
        "tips": (
            "Log into the Google/YouTube account in the SAME browser profile you export from. "
            "Open a normal youtube.com watch page (not Incognito). "
            "Export cookies for .youtube.com (and google.com if prompted). "
            "YouTube cookies expire often — re-export when 403/login errors return."
        ),
    },
    "instagram": {
        "label": "Instagram",
        "sites": ["https://www.instagram.com"],
        "tips": (
            "Log into instagram.com in a normal browser tab, then export. "
            "Instagram sessions die quickly after password changes or security challenges."
        ),
    },
    "x": {
        "label": "X (Twitter)",
        "sites": ["https://x.com", "https://twitter.com"],
        "tips": (
            "Log into x.com, then export cookies for .x.com (and .twitter.com if present)."
        ),
    },
    "twitter": {
        "label": "X (Twitter)",
        "sites": ["https://x.com", "https://twitter.com"],
        "tips": (
            "Log into x.com, then export cookies for .x.com (and .twitter.com if present)."
        ),
    },
    "tiktok": {
        "label": "TikTok",
        "sites": ["https://www.tiktok.com"],
        "tips": "Log into tiktok.com in the browser, then export cookies for .tiktok.com.",
    },
    "facebook": {
        "label": "Facebook",
        "sites": ["https://www.facebook.com"],
        "tips": "Log into facebook.com, then export cookies for .facebook.com.",
    },
    "reddit": {
        "label": "Reddit",
        "sites": ["https://www.reddit.com"],
        "tips": "Log into reddit.com, then export cookies for .reddit.com.",
    },
    "pinterest": {
        "label": "Pinterest",
        "sites": ["https://www.pinterest.com"],
        "tips": "Log into pinterest.com, then export cookies for .pinterest.com.",
    },
    "spotify": {
        "label": "Spotify",
        "sites": ["https://open.spotify.com"],
        "tips": "Log into open.spotify.com, then export cookies for .spotify.com.",
    },
    "soundcloud": {
        "label": "SoundCloud",
        "sites": ["https://soundcloud.com"],
        "tips": "Log into soundcloud.com, then export cookies for .soundcloud.com.",
    },
    "bandcamp": {
        "label": "Bandcamp",
        "sites": ["https://bandcamp.com"],
        "tips": "Log into bandcamp.com if needed, then export cookies for .bandcamp.com.",
    },
    "mixcloud": {
        "label": "Mixcloud",
        "sites": ["https://www.mixcloud.com"],
        "tips": "Log into mixcloud.com, then export cookies for .mixcloud.com.",
    },
    "twitch": {
        "label": "Twitch",
        "sites": ["https://www.twitch.tv"],
        "tips": "Log into twitch.tv, then export cookies for .twitch.tv.",
    },
    "vk": {
        "label": "VK",
        "sites": ["https://vk.com", "https://vkvideo.ru"],
        "tips": "Log into vk.com, then export cookies for .vk.com (and .vkvideo.ru if needed).",
    },
    "rutube": {
        "label": "Rutube",
        "sites": ["https://rutube.ru"],
        "tips": "Log into rutube.ru, then export cookies for .rutube.ru.",
    },
}

# Shared “best tools” block — Netscape cookies.txt is what yt-dlp expects.
_COOKIE_EXPORT_TOOLS = (
    "Best way to extract (Netscape cookies.txt):\n"
    "1) Preferred — browser extension “Get cookies.txt LOCALLY” "
    "(exports the format yt-dlp needs; stays on your machine):\n"
    "   Chrome: https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc\n"
    "   Firefox: https://addons.mozilla.org/en-US/firefox/addon/get-cookies-txt-locally/\n"
    "2) Alternative — “cookies.txt” / similar Netscape exporters in the extension store "
    "(must say Netscape / cookies.txt, not JSON-only).\n"
    "3) yt-dlp FAQ (how cookies are used):\n"
    "   https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp\n"
    "Avoid: random online “cookie converters”, shared/public cookies, or exporting from "
    "a different browser profile than the one that is logged in."
)


def cookie_refresh_instructions(service: str | None) -> str:
    """Human-readable how-to for refreshing cookies for a media service."""
    svc = (service or "unknown").lower().strip()
    if svc == "twitter":
        svc = "x"
    guide = _SERVICE_COOKIE_GUIDES.get(svc)
    label = guide["label"] if guide else svc

    lines = [
        f"How to refresh {label} cookies:",
        "• Open the site(s) below in a normal (non-private) browser window and make sure you are logged in:",
    ]
    if guide:
        for site in guide["sites"]:
            lines.append(f"  - {site}")
        if guide.get("tips"):
            lines.append(f"• Tip: {guide['tips']}")
    else:
        lines.append(f"  - (open the {svc} website while logged in)")

    lines.append("")
    lines.append(_COOKIE_EXPORT_TOOLS)
    lines.append("")
    lines.append("Then upload to the bot (admin DM):")
    lines.append(f"1. Export cookies.txt from the extension while on that site.")
    lines.append(
        f"2. Send the file here with caption: /update_cookies {svc if svc != 'unknown' else '<service>'}"
    )
    lines.append(
        f"   or: attach cookies.txt and reply with /update_cookies {svc if svc != 'unknown' else '<service>'}"
    )
    return "\n".join(lines)


def format_cookie_failure_admin_message(
    *,
    url: str,
    result: YtDlpResult,
    service: str | None = None,
) -> str:
    """Build the admin DM body for a cookie/auth download failure."""
    svc = (service or service_name_from_cookies_path(result.cookies_path) or "unknown").lower()
    if svc == "twitter":
        svc = "x"

    if result.cookies_path and not result.cookies_present:
        cookie_status = f"Status: cookies file missing ({result.cookies_path})"
    elif result.cookies_present:
        cookie_status = (
            f"Status: cookies file present but look invalid/expired "
            f"({result.cookies_path})"
        )
    else:
        cookie_status = "Status: no cookies configured for this service"

    error_snip = (result.error or "").strip()
    if len(error_snip) > 400:
        error_snip = error_snip[:400] + "…"

    url_snip = url if len(url) <= 280 else url[:280] + "…"
    guide = _SERVICE_COOKIE_GUIDES.get(svc)
    label = guide["label"] if guide else svc

    parts = [
        f"⚠️ Cookie / auth failure — {label}",
        f"Failed URL: {url_snip}",
        cookie_status,
        f"Error: {error_snip}",
        "",
        cookie_refresh_instructions(svc),
    ]
    text = "\n".join(parts)
    # Telegram hard limit 4096; leave headroom.
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    return text


async def notify_admin_cookie_failure(
    bot,
    *,
    url: str,
    result: YtDlpResult,
    service: str | None = None,
) -> bool:
    """
    DM ADMIN_ID when a download failed due to cookies/auth.

    Rate-limited per service so a flood of bad links does not spam.
    Returns True if a message was sent.
    """
    if not result.cookie_issue or not result.error:
        return False

    svc = service or service_name_from_cookies_path(result.cookies_path)
    if svc == "twitter":
        svc = "x"
    now = time.monotonic()
    last = _last_cookie_notify_at.get(svc, 0.0)
    if now - last < _COOKIE_NOTIFY_COOLDOWN_SEC:
        logging.info(
            "Skipping cookie-failure admin notify for %s (cooldown %.0fs left)",
            svc,
            _COOKIE_NOTIFY_COOLDOWN_SEC - (now - last),
        )
        return False
    _last_cookie_notify_at[svc] = now

    text = format_cookie_failure_admin_message(url=url, result=result, service=svc)
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=text, disable_web_page_preview=True)
        logging.info("Notified admin about cookie failure for service=%s", svc)
        return True
    except Exception as e:
        logging.error("Failed to notify admin about cookie failure: %s", e)
        return False


async def download_file(file: File) -> str:
    """Downloads a Telegram file to a temporary location and returns the path."""
    with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
        file_path = tmp_file.name

    await file.download_to_drive(file_path)
    return file_path


class YtDlpLogger:
    def debug(self, msg):
        pass  # Ignore debug messages

    def warning(self, msg):
        logging.warning(f"yt-dlp: {msg}")

    def error(self, msg):
        logging.error(f"yt-dlp: {msg}")


def download_video_ytdlp(url: str, cookies_path: str = None) -> YtDlpResult:
    """Downloads a video using yt-dlp with a 50MB limit."""

    cookies_present = _cookies_present(cookies_path)

    # Create a temporary file path using uuid
    temp_dir = tempfile.gettempdir()
    temp_filename = f"{uuid.uuid4()}.mp4"
    temp_path = os.path.join(temp_dir, temp_filename)

    outtmpl = temp_path

    ydl_opts = _base_ydl_opts()
    ydl_opts.update(
        {
            "format": (
                "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/"
                "best[height<=720][ext=mp4]/best[height<=720]/best[ext=mp4]/best"
            ),
            "outtmpl": outtmpl,
            "max_filesize": 50 * 1024 * 1024,  # 50MB
        }
    )

    cookie_tmp: str | None = None
    if cookies_present:
        try:
            cookie_tmp, n_cookies, cookie_names = _prepare_cookiefile_copy(cookies_path)
            ydl_opts["cookiefile"] = cookie_tmp
            session = [n for n in cookie_names if n.lower() in _SESSION_COOKIE_NAMES]
            logging.info(
                "yt-dlp video cookies source=%s rows=%d session=%s",
                cookies_path,
                n_cookies,
                ",".join(session) if session else "none",
            )
            if n_cookies == 0:
                logging.warning(
                    "Cookie file %s has zero valid rows after normalize", cookies_path
                )
        except Exception as e:
            logging.error("Failed to prepare cookiefile %s: %s", cookies_path, e)
    elif cookies_path:
        logging.warning(
            "Cookies expected at %s but file is missing; downloading without cookies",
            cookies_path,
        )

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # yt-dlp might append the extension if not strictly enforced or if merging happens.
        # We use glob to find the file regardless of extension.
        possible_files = glob.glob(f"{temp_path}*")
        if possible_files:
            return YtDlpResult(
                path=possible_files[0],
                cookies_path=cookies_path,
                cookies_present=cookies_present,
            )

        error = "download finished but no output file was produced"
        return YtDlpResult(
            error=error,
            cookie_issue=_detect_cookie_issue(error, cookies_path, cookies_present),
            cookies_path=cookies_path,
            cookies_present=cookies_present,
        )

    except Exception as e:
        error = str(e)
        logging.error("yt-dlp video failed url=%s error=%s", url, e)
        return YtDlpResult(
            error=error,
            cookie_issue=_detect_cookie_issue(error, cookies_path, cookies_present),
            cookies_path=cookies_path,
            cookies_present=cookies_present,
        )
    finally:
        if cookie_tmp and os.path.exists(cookie_tmp):
            try:
                os.remove(cookie_tmp)
            except OSError:
                pass


def download_audio_ytdlp(url: str, cookies_path: str = None) -> YtDlpResult:
    """Downloads audio using yt-dlp and returns metadata in result.info."""

    cookies_present = _cookies_present(cookies_path)

    # Create a temporary file path using uuid
    temp_dir = tempfile.gettempdir()
    temp_filename = f"{uuid.uuid4()}.mp3"
    temp_path = os.path.join(temp_dir, temp_filename)

    # Output template for yt-dlp
    base_path = os.path.splitext(temp_path)[0]
    outtmpl = base_path + ".%(ext)s"

    ydl_opts = _base_ydl_opts()
    ydl_opts.update(
        {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "max_filesize": 50 * 1024 * 1024,  # 50MB
            "writethumbnail": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                },
            ],
        }
    )

    cookie_tmp: str | None = None
    if cookies_present:
        try:
            cookie_tmp, n_cookies, cookie_names = _prepare_cookiefile_copy(cookies_path)
            ydl_opts["cookiefile"] = cookie_tmp
            session = [n for n in cookie_names if n.lower() in _SESSION_COOKIE_NAMES]
            logging.info(
                "yt-dlp audio cookies source=%s rows=%d session=%s",
                cookies_path,
                n_cookies,
                ",".join(session) if session else "none",
            )
        except Exception as e:
            logging.error("Failed to prepare cookiefile %s: %s", cookies_path, e)
    elif cookies_path:
        logging.warning(
            "Cookies expected at %s but file is missing; downloading without cookies",
            cookies_path,
        )

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        # The file should be at base_path + ".mp3"
        audio_path = base_path + ".mp3"
        if not os.path.exists(audio_path):
            # Fallback
            if os.path.exists(temp_path):
                audio_path = temp_path
            else:
                error = "audio download finished but no output file was produced"
                return YtDlpResult(
                    error=error,
                    cookie_issue=_detect_cookie_issue(
                        error, cookies_path, cookies_present
                    ),
                    cookies_path=cookies_path,
                    cookies_present=cookies_present,
                )

        # Find thumbnail
        # yt-dlp writes thumbnail to base_path + .jpg or .webp etc.
        thumbnail_path = None
        for ext in [".jpg", ".jpeg", ".png", ".webp"]:
            possible_thumb = base_path + ext
            if os.path.exists(possible_thumb):
                thumbnail_path = possible_thumb
                break

        return YtDlpResult(
            info={
                "audio_path": audio_path,
                "title": info.get("title", "Unknown Title"),
                "description": info.get("description", ""),
                "thumbnail_path": thumbnail_path,
                "duration": info.get("duration"),
                "uploader": info.get("uploader"),
            },
            cookies_path=cookies_path,
            cookies_present=cookies_present,
        )

    except Exception as e:
        error = str(e)
        logging.error("yt-dlp audio failed url=%s error=%s", url, e)
        return YtDlpResult(
            error=error,
            cookie_issue=_detect_cookie_issue(error, cookies_path, cookies_present),
            cookies_path=cookies_path,
            cookies_present=cookies_present,
        )
    finally:
        if cookie_tmp and os.path.exists(cookie_tmp):
            try:
                os.remove(cookie_tmp)
            except OSError:
                pass


def extract_frames_from_video(video_path: str, max_frames: int = 5) -> list[bytes]:
    """Extracts representative frames from a video/animation."""
    frames = []
    cap = cv2.VideoCapture(video_path)

    if not cap.isOpened():
        logging.error(f"Could not open video file: {video_path}")
        return []

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total_frames <= 0:
        # Fallback if frame count is unknown
        logging.warning("Could not determine total frames, reading first few.")
        total_frames = max_frames * 10  # Guess

    # Calculate indices to capture
    indices = [int(i * total_frames / max_frames) for i in range(max_frames)]
    indices = sorted(list(set(indices)))  # Remove duplicates and sort

    current_frame = 0
    captured_count = 0

    while cap.isOpened() and captured_count < len(indices):
        ret, frame = cap.read()
        if not ret:
            break

        if current_frame in indices:
            # Convert to JPEG bytes
            ret, buffer = cv2.imencode(".jpg", frame)
            if ret:
                frames.append(buffer.tobytes())
                captured_count += 1

        current_frame += 1

    cap.release()
    return frames
