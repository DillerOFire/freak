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
            [
                "uv",
                "pip",
                "install",
                "-U",
                "git+https://github.com/yt-dlp/yt-dlp.git@master",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        if process.returncode == 0:
            stdout = process.stdout.strip()
            logging.info(f"yt-dlp update output: {stdout}")
            if "Installed" in stdout or "upgraded" in stdout:
                return True, f"yt-dlp updated successfully:\n{stdout}"
            return True, "yt-dlp is already up to date."
        else:
            stderr = process.stderr.strip()
            logging.error(f"yt-dlp update failed: {stderr}")
            return False, f"yt-dlp update failed:\n{stderr}"

    except Exception as e:
        logging.error(f"An error occurred while updating yt-dlp: {e}")
        return False, f"An error occurred: {e}"


async def check_for_updates() -> bool:
    """
    Checks if there are updates available in the git repository.
    Returns True if updates are available.
    """
    try:
        logging.info("Checking for git updates...")
        subprocess.run(["git", "fetch"], check=True, capture_output=True)

        # Check if HEAD is behind @{u}
        process = subprocess.run(
            ["git", "status", "-uno"], capture_output=True, text=True, check=True
        )

        if "Your branch is behind" in process.stdout:
            logging.info("Updates available.")
            return True

        logging.info("No updates available.")
        return False
    except Exception as e:
        logging.error(f"Failed to check for updates: {e}")
        return False


async def pull_updates() -> tuple[bool, str]:
    """
    Pulls updates from the git repository.
    Returns (success, message).
    """
    try:
        logging.info("Pulling updates...")
        process = subprocess.run(
            ["git", "pull"], capture_output=True, text=True, check=False
        )

        if process.returncode == 0:
            return True, f"Successfully pulled updates:\n{process.stdout}"
        else:
            return False, f"Failed to pull updates:\n{process.stderr}"
    except Exception as e:
        logging.error(f"Failed to pull updates: {e}")
        return False, f"An error occurred while pulling: {e}"


def restart_bot():
    """
    Restarts the bot by exiting with status 0.
    Assumes a supervisor (like systemd) will restart it.
    """
    import sys

    logging.info("Restarting bot...")
    sys.exit(0)
