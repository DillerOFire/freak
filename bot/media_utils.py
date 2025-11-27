import cv2
import tempfile
import logging
import os
import yt_dlp
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
        "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]",
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

        if os.path.exists(temp_path):
            return temp_path
        elif os.path.exists(temp_path + ".mp4"):
            return temp_path + ".mp4"
        elif os.path.exists(temp_path + ".mkv"):
            return temp_path + ".mkv"

        # If we are here, maybe it failed or used another extension
        return None

    except Exception as e:
        logging.error(f"yt-dlp failed for {url}: {e}")
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
