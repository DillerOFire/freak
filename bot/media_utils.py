import asyncio
import cv2
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import yt_dlp
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote, urlsplit

from telegram import File, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from config import (
    ADMIN_ID,
    COOKIES_DIR,
    WEB_SETTINGS_URL,
    YTDLP_DOWNLOAD_TIMEOUT_SEC,
    YTDLP_MAX_CONCURRENT_DOWNLOADS,
    YTDLP_QUEUE_TIMEOUT_SEC,
    YTDLP_SOCKET_TIMEOUT_SEC,
)

TELEGRAM_MEDIA_LIMIT_BYTES = 50 * 1024 * 1024

# Rate-limit cookie failure DMs per service (seconds).
_COOKIE_NOTIFY_COOLDOWN_SEC = 15 * 60
_last_cookie_notify_at: dict[str, float] = {}

# Substrings in yt-dlp errors that strongly indicate cookies/auth need refresh.
_COOKIE_FAILURE_MARKERS = (
    "sign in",
    "login required",
    "please log in",
    "cookies are required",
    "invalid cookies",
    "cookie authentication",
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
    "authentication required",
)


class YtDlpFailureKind(StrEnum):
    NETWORK = "network"
    AUTH = "auth"
    FORMAT = "format"
    TOO_LARGE = "too_large"
    TIMEOUT = "timeout"
    QUEUE_FULL = "queue_full"
    POSTPROCESS = "postprocess"
    UNSUPPORTED = "unsupported"
    INTERNAL = "internal"


@dataclass
class YtDlpResult:
    """Outcome of a yt-dlp video/audio download attempt."""

    path: str | None = None
    info: dict | None = None
    error: str | None = None
    failure_kind: YtDlpFailureKind | None = None
    # Compatibility for older callers/tests; failure_kind is authoritative.
    cookie_issue: bool = False
    cookies_path: str | None = None
    cookies_present: bool = False
    work_dir: str | None = None
    extractor: str | None = None
    elapsed_seconds: float = 0.0
    size_bytes: int | None = None
    ytdlp_version: str = ""

    def __post_init__(self) -> None:
        if self.cookie_issue and self.failure_kind is None:
            self.failure_kind = YtDlpFailureKind.AUTH
        self.cookie_issue = self.failure_kind == YtDlpFailureKind.AUTH

    @property
    def ok(self) -> bool:
        return self.path is not None or self.info is not None

    def cleanup(self) -> None:
        """Remove every artifact owned by this result; safe to call repeatedly."""
        if self.work_dir:
            shutil.rmtree(self.work_dir, ignore_errors=True)
            self.work_dir = None


@dataclass(frozen=True)
class MediaServicePolicy:
    name: str
    hosts: tuple[str, ...]
    cookie_service: str


_MEDIA_SERVICE_POLICIES = (
    MediaServicePolicy("youtube", ("youtube.com", "youtu.be"), "youtube"),
    MediaServicePolicy("instagram", ("instagram.com",), "instagram"),
    MediaServicePolicy("x", ("x.com", "twitter.com"), "x"),
    MediaServicePolicy("tiktok", ("tiktok.com",), "tiktok"),
    MediaServicePolicy("facebook", ("facebook.com",), "facebook"),
    MediaServicePolicy("reddit", ("reddit.com",), "reddit"),
    MediaServicePolicy("pinterest", ("pinterest.com",), "pinterest"),
    MediaServicePolicy("spotify", ("spotify.com",), "spotify"),
    MediaServicePolicy("soundcloud", ("soundcloud.com",), "soundcloud"),
    MediaServicePolicy("bandcamp", ("bandcamp.com",), "bandcamp"),
    MediaServicePolicy("mixcloud", ("mixcloud.com",), "mixcloud"),
    MediaServicePolicy("twitch", ("twitch.tv",), "twitch"),
    MediaServicePolicy("vk", ("vk.com", "vkvideo.ru"), "vk"),
    MediaServicePolicy("rutube", ("rutube.ru",), "rutube"),
)


def resolve_media_service(url: str) -> MediaServicePolicy | None:
    """Resolve an exact host/subdomain to its shared download policy."""
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    for policy in _MEDIA_SERVICE_POLICIES:
        if any(host == domain or host.endswith(f".{domain}") for domain in policy.hosts):
            return policy
    return None


def cookies_path_for_url(url: str) -> str | None:
    policy = resolve_media_service(url)
    if not policy:
        return None
    return os.path.join(COOKIES_DIR, f"{policy.cookie_service}.txt")


def extract_supported_media_urls(text: str) -> list[str]:
    urls: list[str] = []
    for raw_url in re.findall(r"https?://[^\s<>()]+", text):
        url = raw_url.rstrip(".,!?;:'\"]}")
        if resolve_media_service(url):
            urls.append(url)
    return urls


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
    return marker_hit


def _classify_ytdlp_failure(error: str | None) -> YtDlpFailureKind:
    message = (error or "").lower()
    if "freak-size-limit" in message or "larger than max-filesize" in message:
        return YtDlpFailureKind.TOO_LARGE
    if "freak-download-timeout" in message or "timed out" in message:
        return YtDlpFailureKind.TIMEOUT
    if any(marker in message for marker in _COOKIE_FAILURE_MARKERS):
        return YtDlpFailureKind.AUTH
    if "unsupported url" in message or "no suitable extractor" in message:
        return YtDlpFailureKind.UNSUPPORTED
    if "requested format is not available" in message or "no video formats" in message:
        return YtDlpFailureKind.FORMAT
    if "ffmpeg" in message or "postprocessing" in message or "post-processing" in message:
        return YtDlpFailureKind.POSTPROCESS
    if any(marker in message for marker in ("http error", "network", "connection", "temporary failure")):
        return YtDlpFailureKind.NETWORK
    return YtDlpFailureKind.INTERNAL


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
        # Netscape marks HttpOnly cookies with this otherwise-comment-looking
        # prefix.  Dropping those rows loses common logged-in sessions.
        http_only_prefix = ""
        stripped = line.lstrip()
        if stripped.startswith("#HttpOnly_"):
            http_only_prefix = "#HttpOnly_"
            line = stripped[len(http_only_prefix) :]
        elif stripped.startswith("#"):
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
        out_lines.append(http_only_prefix + "\t".join([domain, flag, path, secure, expires, name, value]))
        names.append(name)

    text = "\n".join(out_lines)
    if text and not text.endswith("\n"):
        text += "\n"
    if not text.startswith("# Netscape HTTP Cookie File"):
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
    # Never leave a partially pasted/exported jar in place if the process is
    # interrupted.  mkstemp defaults to a private (0600) file.
    fd, tmp_path = tempfile.mkstemp(prefix=".cookies-", suffix=".txt", dir=parent or None)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(normalized)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    session = [n for n in names if n.lower() in _SESSION_COOKIE_NAMES]
    return len(names), names, session


def _prepare_cookiefile_copy(
    cookies_path: str, temp_dir: str | None = None
) -> tuple[str, int, list[str]]:
    """
    Build a temp Netscape cookiefile for yt-dlp.

    Uses a copy so yt-dlp cannot rewrite/empty the stored jar on download.
    Normalizes space-separated exports to tabs.
    """
    with open(cookies_path, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    normalized, names = normalize_netscape_cookies(raw)
    fd, tmp_path = tempfile.mkstemp(
        prefix="ytdlp-cookies-", suffix=".txt", dir=temp_dir
    )
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(normalized)
    return tmp_path, len(names), names


class _DownloadGuard:
    """Cooperatively enforces the aggregate byte cap and job deadline."""

    def __init__(self, cancel_event: threading.Event | None = None) -> None:
        self.cancel_event = cancel_event or threading.Event()
        self.started_at = time.monotonic()
        self.deadline = self.started_at + YTDLP_DOWNLOAD_TIMEOUT_SEC
        self._downloaded_by_file: dict[str, int] = {}

    def check(self) -> None:
        if self.cancel_event.is_set() or time.monotonic() >= self.deadline:
            raise yt_dlp.utils.DownloadError("FREAK-DOWNLOAD-TIMEOUT")

    def progress_hook(self, status: dict) -> None:
        self.check()
        filename = str(status.get("filename") or status.get("tmpfilename") or "stream")
        downloaded = int(status.get("downloaded_bytes") or 0)
        self._downloaded_by_file[filename] = max(
            downloaded, self._downloaded_by_file.get(filename, 0)
        )
        if sum(self._downloaded_by_file.values()) > TELEGRAM_MEDIA_LIMIT_BYTES:
            raise yt_dlp.utils.DownloadError("FREAK-SIZE-LIMIT")

    def match_filter(self, info: dict, *, incomplete: bool) -> str | None:
        self.check()
        formats = info.get("requested_formats") or [info]
        known_sizes = [
            fmt.get("filesize") or fmt.get("filesize_approx")
            for fmt in formats
            if isinstance(fmt, dict)
        ]
        if known_sizes and all(size is not None for size in known_sizes):
            if sum(int(size) for size in known_sizes) > TELEGRAM_MEDIA_LIMIT_BYTES:
                return "FREAK-SIZE-LIMIT"
        return None


def _base_ydl_opts(guard: _DownloadGuard | None = None) -> dict:
    """Shared, bounded yt-dlp options including YouTube EJS support."""
    opts = {
        "quiet": True,
        "noplaylist": True,
        "socket_timeout": YTDLP_SOCKET_TIMEOUT_SEC,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "file_access_retries": 3,
        "concurrent_fragment_downloads": 2,
        "retry_sleep_functions": {
            "http": lambda attempt: min(2 ** max(attempt - 1, 0), 8),
            "fragment": lambda attempt: min(2 ** max(attempt - 1, 0), 8),
            "extractor": lambda attempt: min(attempt, 3),
        },
        # Deno is installed in the Docker image; node listed as optional fallback.
        "js_runtimes": {"deno": {}, "node": {}},
        # Allow fetching challenge solver scripts when the package needs them.
        "remote_components": {"ejs:github"},
        "logger": YtDlpLogger(),
    }
    if guard:
        opts.update(
            {
                "progress_hooks": [guard.progress_hook],
                "postprocessor_hooks": [lambda _status: guard.check()],
                "match_filter": guard.match_filter,
            }
        )
    return opts


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
    reply_markup = None
    if WEB_SETTINGS_URL.startswith("https://"):
        cookie_url = WEB_SETTINGS_URL.rstrip("/") + "/cookies?service=" + quote(svc, safe="")
        reply_markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Refresh cookies in web app", web_app=WebAppInfo(cookie_url))]]
        )
    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=text,
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
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


def _ytdlp_version() -> str:
    return str(getattr(yt_dlp.version, "__version__", "unknown"))


def _apply_cookie_options(
    opts: dict, cookies_path: str | None, work_dir: str, media_type: str
) -> bool:
    cookies_present = _cookies_present(cookies_path)
    if not cookies_present:
        if cookies_path:
            logging.warning("Cookies expected at %s but file is missing", cookies_path)
        return False
    try:
        cookie_tmp, count, names = _prepare_cookiefile_copy(cookies_path, work_dir)
        if not count:
            logging.warning("Cookie file %s has zero valid rows", cookies_path)
            return True
        opts["cookiefile"] = cookie_tmp
        session = [name for name in names if name.lower() in _SESSION_COOKIE_NAMES]
        logging.info(
            "yt-dlp %s cookies source=%s rows=%d session=%s",
            media_type,
            cookies_path,
            count,
            ",".join(session) if session else "none",
        )
    except Exception as exc:
        logging.error("Failed to prepare cookiefile %s: %s", cookies_path, exc)
    return True


def _find_artifact(work_dir: str, suffixes: set[str]) -> str | None:
    candidates = [
        path
        for path in Path(work_dir).iterdir()
        if path.is_file()
        and path.suffix.lower() in suffixes
        and not path.name.endswith((".part", ".ytdl"))
    ]
    if not candidates:
        return None
    return str(max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.stat().st_size)))


def _failure_result(
    *,
    error: str,
    cookies_path: str | None,
    cookies_present: bool,
    work_dir: str | None,
    started_at: float,
    failure_kind: YtDlpFailureKind | None = None,
    extractor: str | None = None,
) -> YtDlpResult:
    kind = failure_kind or _classify_ytdlp_failure(error)
    if work_dir:
        shutil.rmtree(work_dir, ignore_errors=True)
    return YtDlpResult(
        error=error,
        failure_kind=kind,
        cookies_path=cookies_path,
        cookies_present=cookies_present,
        extractor=extractor,
        elapsed_seconds=time.monotonic() - started_at,
        ytdlp_version=_ytdlp_version(),
    )


def download_video_ytdlp(
    url: str,
    cookies_path: str | None = None,
    *,
    cancel_event: threading.Event | None = None,
) -> YtDlpResult:
    """Download one video into an isolated directory with a hard 50 MiB cap."""
    started_at = time.monotonic()
    work_dir = tempfile.mkdtemp(prefix="freak-ytdlp-video-")
    guard = _DownloadGuard(cancel_event)
    cookies_present = _cookies_present(cookies_path)
    opts = _base_ydl_opts(guard)
    opts.update(
        {
            "format": (
                "bestvideo[height<=720][ext=mp4][filesize<=50M]+"
                "bestaudio[ext=m4a][filesize<=50M]/"
                "best[height<=720][ext=mp4][filesize<=50M]/"
                "best[height<=720][filesize<=50M]/"
                "best[height<=720][ext=mp4][filesize_approx<=50M]/"
                "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/"
                "best[height<=720][ext=mp4]/best[height<=720]/best[ext=mp4]/best"
            ),
            "outtmpl": str(Path(work_dir) / "media.%(ext)s"),
            "max_filesize": TELEGRAM_MEDIA_LIMIT_BYTES,
        }
    )
    _apply_cookie_options(opts, cookies_path, work_dir, "video")
    info: dict = {}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True) or {}
        output = _find_artifact(work_dir, {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"})
        if not output:
            raise yt_dlp.utils.DownloadError("download finished but no video output was produced")
        size = os.path.getsize(output)
        if size > TELEGRAM_MEDIA_LIMIT_BYTES:
            raise yt_dlp.utils.DownloadError("FREAK-SIZE-LIMIT: final video exceeds 50 MiB")
        return YtDlpResult(
            path=output,
            cookies_path=cookies_path,
            cookies_present=cookies_present,
            work_dir=work_dir,
            extractor=info.get("extractor_key") or info.get("extractor"),
            elapsed_seconds=time.monotonic() - started_at,
            size_bytes=size,
            ytdlp_version=_ytdlp_version(),
        )
    except Exception as exc:
        error = str(exc)
        kind = _classify_ytdlp_failure(error)
        logging.error(
            "yt-dlp video failed kind=%s version=%s elapsed=%.2fs url=%s error=%s",
            kind,
            _ytdlp_version(),
            time.monotonic() - started_at,
            url,
            error,
        )
        return _failure_result(
            error=error,
            cookies_path=cookies_path,
            cookies_present=cookies_present,
            work_dir=work_dir,
            started_at=started_at,
            failure_kind=kind,
            extractor=info.get("extractor_key") or info.get("extractor"),
        )


def download_audio_ytdlp(
    url: str,
    cookies_path: str | None = None,
    *,
    cancel_event: threading.Event | None = None,
) -> YtDlpResult:
    """Download and convert one audio item in an isolated 50 MiB workspace."""
    started_at = time.monotonic()
    work_dir = tempfile.mkdtemp(prefix="freak-ytdlp-audio-")
    guard = _DownloadGuard(cancel_event)
    cookies_present = _cookies_present(cookies_path)
    opts = _base_ydl_opts(guard)
    opts.update(
        {
            "format": (
                "bestaudio[filesize<=50M]/bestaudio[filesize_approx<=50M]/"
                "bestaudio/best"
            ),
            "outtmpl": str(Path(work_dir) / "media.%(ext)s"),
            "max_filesize": TELEGRAM_MEDIA_LIMIT_BYTES,
            "writethumbnail": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }
    )
    _apply_cookie_options(opts, cookies_path, work_dir, "audio")
    info: dict = {}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True) or {}
        audio_path = _find_artifact(work_dir, {".mp3"})
        if not audio_path:
            raise yt_dlp.utils.DownloadError("download finished but no audio output was produced")
        size = os.path.getsize(audio_path)
        if size > TELEGRAM_MEDIA_LIMIT_BYTES:
            raise yt_dlp.utils.DownloadError("FREAK-SIZE-LIMIT: final audio exceeds 50 MiB")
        thumbnail_path = _find_artifact(work_dir, {".jpg", ".jpeg", ".png", ".webp"})
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
            work_dir=work_dir,
            extractor=info.get("extractor_key") or info.get("extractor"),
            elapsed_seconds=time.monotonic() - started_at,
            size_bytes=size,
            ytdlp_version=_ytdlp_version(),
        )
    except Exception as exc:
        error = str(exc)
        kind = _classify_ytdlp_failure(error)
        logging.error(
            "yt-dlp audio failed kind=%s version=%s elapsed=%.2fs url=%s error=%s",
            kind,
            _ytdlp_version(),
            time.monotonic() - started_at,
            url,
            error,
        )
        return _failure_result(
            error=error,
            cookies_path=cookies_path,
            cookies_present=cookies_present,
            work_dir=work_dir,
            started_at=started_at,
            failure_kind=kind,
            extractor=info.get("extractor_key") or info.get("extractor"),
        )


class YtDlpManager:
    """Bounded async facade around yt-dlp's blocking Python API."""

    def __init__(self, max_concurrent: int = YTDLP_MAX_CONCURRENT_DOWNLOADS) -> None:
        cleanup_stale_ytdlp_artifacts()
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._chat_locks: dict[int, asyncio.Lock] = {}

    def _chat_lock(self, chat_id: int) -> asyncio.Lock:
        return self._chat_locks.setdefault(chat_id, asyncio.Lock())

    async def _run(self, func, url: str, chat_id: int) -> YtDlpResult:
        policy = resolve_media_service(url)
        if not policy:
            return YtDlpResult(
                error="Unsupported media URL",
                failure_kind=YtDlpFailureKind.UNSUPPORTED,
                ytdlp_version=_ytdlp_version(),
            )
        cookies_path = cookies_path_for_url(url)
        chat_lock = self._chat_lock(chat_id)
        try:
            await asyncio.wait_for(chat_lock.acquire(), timeout=YTDLP_QUEUE_TIMEOUT_SEC)
        except TimeoutError:
            return YtDlpResult(
                error="Download queue wait timed out",
                failure_kind=YtDlpFailureKind.QUEUE_FULL,
                ytdlp_version=_ytdlp_version(),
            )
        try:
            try:
                await asyncio.wait_for(
                    self._semaphore.acquire(), timeout=YTDLP_QUEUE_TIMEOUT_SEC
                )
            except TimeoutError:
                return YtDlpResult(
                    error="Download queue is busy",
                    failure_kind=YtDlpFailureKind.QUEUE_FULL,
                    ytdlp_version=_ytdlp_version(),
                )
            try:
                cancel_event = threading.Event()
                worker = asyncio.create_task(
                    asyncio.to_thread(
                        func, url, cookies_path, cancel_event=cancel_event
                    )
                )
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(worker), timeout=YTDLP_DOWNLOAD_TIMEOUT_SEC
                    )
                except TimeoutError:
                    cancel_event.set()
                    result = await worker
                    result.cleanup()
                    return YtDlpResult(
                        error="Download exceeded its time limit",
                        failure_kind=YtDlpFailureKind.TIMEOUT,
                        cookies_path=cookies_path,
                        cookies_present=_cookies_present(cookies_path),
                        ytdlp_version=_ytdlp_version(),
                    )
                except asyncio.CancelledError:
                    cancel_event.set()
                    result = await asyncio.shield(worker)
                    result.cleanup()
                    raise
            finally:
                self._semaphore.release()
        finally:
            chat_lock.release()

    async def download_video(self, url: str, chat_id: int) -> YtDlpResult:
        return await self._run(download_video_ytdlp, url, chat_id)

    async def download_audio(self, url: str, chat_id: int) -> YtDlpResult:
        return await self._run(download_audio_ytdlp, url, chat_id)


def cleanup_stale_ytdlp_artifacts(max_age_seconds: int = 6 * 60 * 60) -> int:
    """Remove abandoned Freak yt-dlp workspaces left by a crashed process."""
    cutoff = time.time() - max_age_seconds
    removed = 0
    temp_root = Path(tempfile.gettempdir())
    for pattern in ("freak-ytdlp-video-*", "freak-ytdlp-audio-*", "ytdlp-cookies-*"):
        for path in temp_root.glob(pattern):
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.is_file():
                    path.unlink()
                removed += 1
            except OSError:
                logging.warning("Could not remove stale yt-dlp artifact %s", path)
    if removed:
        logging.info("Removed %d stale yt-dlp artifact(s)", removed)
    return removed


ytdlp_manager = YtDlpManager()


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
