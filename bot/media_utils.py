import cv2
import tempfile
import logging
import os
import yt_dlp
import glob
from telegram import File


async def download_file(file: File) -> str:
    """Downloads a Telegram file to a temporary location and returns the path."""
    with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
        file_path = tmp_file.name

    await file.download_to_drive(file_path)
    return file_path


def download_video_ytdlp(url: str, cookies_path: str = None) -> str | None:
    """Downloads a video using yt-dlp with a 50MB limit."""

    # Create a temporary file path pattern
    # yt-dlp adds extension automatically, so we'll rename it later or let it be
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp_file:
        temp_path = tmp_file.name

    # We close it because yt-dlp needs to write to it (or replace it)
    os.remove(temp_path)

    # Output template for yt-dlp
    outtmpl = temp_path

    ydl_opts = {
        "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]/best[ext=mp4]/best",
        "outtmpl": outtmpl,
        "max_filesize": 50 * 1024 * 1024,  # 50MB
        "quiet": True,
        "noplaylist": True,
        # Enable remote components to solve n-challenge (requires ejs)
        "remote_components": {"ejs:github"},
    }

    if cookies_path and os.path.exists(cookies_path):
        ydl_opts["cookiefile"] = cookies_path

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # yt-dlp might append the extension if not strictly enforced or if merging happens.
        # However, since we specified outtmpl as a filename (without extension template),
        # it *should* try to use that name. But if merging happens, it might be tricky.
        # Let's check if the file exists, if not check for .mp4 appended

        # yt-dlp might append the extension if not strictly enforced or if merging happens.
        # We use glob to find the file regardless of extension.
        possible_files = glob.glob(f"{temp_path}*")
        if possible_files:
            return possible_files[0]

        # If we are here, maybe it failed or used another extension
        return None

    except Exception as e:
        logging.error(f"yt-dlp failed for {url}: {e}")
        return None


def download_audio_ytdlp(url: str, cookies_path: str = None) -> dict | None:
    """Downloads audio using yt-dlp and returns metadata."""

    # Create a temporary file path pattern
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp_file:
        temp_path = tmp_file.name

    # We close it because yt-dlp needs to write to it (or replace it)
    os.remove(temp_path)

    # Output template for yt-dlp
    base_path = os.path.splitext(temp_path)[0]
    outtmpl = base_path + ".%(ext)s"

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "max_filesize": 50 * 1024 * 1024,  # 50MB
        "quiet": True,
        "noplaylist": True,
        "writethumbnail": True,  # Download thumbnail
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            },
            # Embed thumbnail in the audio file if possible (optional, but good)
            # {'key': 'EmbedThumbnail'},
            # We will handle thumbnail separately for Telegram
        ],
        "remote_components": {"ejs:github"},
    }

    if cookies_path and os.path.exists(cookies_path):
        ydl_opts["cookiefile"] = cookies_path

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
                return None

        # Find thumbnail
        # yt-dlp writes thumbnail to base_path + .jpg or .webp etc.
        thumbnail_path = None
        for ext in [".jpg", ".jpeg", ".png", ".webp"]:
            possible_thumb = base_path + ext
            if os.path.exists(possible_thumb):
                thumbnail_path = possible_thumb
                break

        # If not found locally, maybe we can use the URL from info (but sending URL to telegram might fail if it's not direct)
        # For now, if no local thumbnail, we just send None.

        return {
            "audio_path": audio_path,
            "title": info.get("title", "Unknown Title"),
            "description": info.get("description", ""),
            "thumbnail_path": thumbnail_path,
            "duration": info.get("duration"),
            "uploader": info.get("uploader"),
        }

    except Exception as e:
        logging.error(f"yt-dlp audio download failed for {url}: {e}")
        return None


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
