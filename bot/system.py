import subprocess
import logging


async def update_ytdlp_package() -> tuple[bool, str]:
    """
    Updates the yt-dlp package using uv pip install -U yt-dlp.
    Returns (success, message).
    """
    try:
        logging.info("Attempting to update yt-dlp...")
        # running in non-interactive mode
        process = subprocess.run(
            ["uv", "pip", "install", "-U", "yt-dlp"],
            capture_output=True,
            text=True,
            check=False,
        )

        if process.returncode == 0:
            stdout = process.stdout.strip()
            logging.info(f"yt-dlp update output: {stdout}")
            if "Requirement already satisfied" in stdout:
                return True, "yt-dlp is already up to date."
            return True, f"yt-dlp updated successfully:\n{stdout}"
        else:
            stderr = process.stderr.strip()
            logging.error(f"yt-dlp update failed: {stderr}")
            return False, f"yt-dlp update failed:\n{stderr}"

    except Exception as e:
        logging.error(f"An error occurred while updating yt-dlp: {e}")
        return False, f"An error occurred: {e}"
