import cv2
import tempfile
import logging
from telegram import File


async def download_file(file: File) -> str:
    """Downloads a Telegram file to a temporary location and returns the path."""
    with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
        file_path = tmp_file.name

    await file.download_to_drive(file_path)
    return file_path


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
