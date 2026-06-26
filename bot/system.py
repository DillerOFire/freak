import logging
import os
import shutil
import subprocess
from functools import lru_cache

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_STARTUP_CHECK = (
    "from bot.handlers import handle_message; "
    "from bot.agent import run_ponder_agent"
)


@lru_cache(maxsize=1)
def resolve_uv_executable() -> str:
    """Locate uv when PATH is minimal (e.g. systemd services)."""
    override = os.environ.get("UV_EXECUTABLE", "").strip()
    if override:
        if os.path.isfile(override) and os.access(override, os.X_OK):
            return override
        raise FileNotFoundError(f"UV_EXECUTABLE is set but not executable: {override}")

    which_uv = shutil.which("uv")
    if which_uv:
        return which_uv

    home = os.path.expanduser("~")
    for candidate in (
        os.path.join(home, ".local", "bin", "uv"),
        os.path.join(home, ".cargo", "bin", "uv"),
        "/usr/local/bin/uv",
        "/usr/bin/uv",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    raise FileNotFoundError(
        "uv executable not found. Install uv or set UV_EXECUTABLE in the environment."
    )


def _uv_cmd(*args: str) -> list[str]:
    return [resolve_uv_executable(), *args]


def _run_cmd(args: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=check,
        cwd=PROJECT_ROOT,
    )


async def update_ytdlp_package() -> tuple[bool, str]:
    """
    Updates the yt-dlp package using uv pip install -U yt-dlp.
    Returns (success, message).
    """
    try:
        logging.info("Attempting to update yt-dlp...")
        process = _run_cmd(
            _uv_cmd(
                "pip",
                "install",
                "-U",
                "git+https://github.com/yt-dlp/yt-dlp.git@master",
            ),
            check=False,
        )

        if process.returncode == 0:
            stdout = process.stdout.strip()
            logging.info(f"yt-dlp update output: {stdout}")
            if "Installed" in stdout or "upgraded" in stdout:
                return True, f"yt-dlp updated successfully:\n{stdout}"
            return True, "yt-dlp is already up to date."
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
        _run_cmd(["git", "fetch"], check=True)

        process = _run_cmd(["git", "status", "-uno"], check=True)

        if "Your branch is behind" in process.stdout:
            logging.info("Updates available.")
            return True

        logging.info("No updates available.")
        return False
    except Exception as e:
        logging.error(f"Failed to check for updates: {e}")
        return False


async def get_git_revision() -> str | None:
    try:
        process = _run_cmd(["git", "rev-parse", "HEAD"], check=True)
        revision = process.stdout.strip()
        return revision or None
    except Exception as e:
        logging.error(f"Failed to read git revision: {e}")
        return None


async def pull_updates() -> tuple[bool, str]:
    """
    Pulls updates from the git repository.
    Returns (success, message).
    """
    try:
        logging.info("Pulling updates...")
        process = _run_cmd(["git", "pull"], check=False)

        if process.returncode == 0:
            return True, f"Successfully pulled updates:\n{process.stdout.strip()}"
        return False, f"Failed to pull updates:\n{process.stderr.strip()}"
    except Exception as e:
        logging.error(f"Failed to pull updates: {e}")
        return False, f"An error occurred while pulling: {e}"


async def sync_dependencies() -> tuple[bool, str]:
    """Install project dependencies from pyproject.toml / uv.lock."""
    try:
        logging.info("Syncing Python dependencies with uv...")
        process = _run_cmd(_uv_cmd("sync"), check=False)

        if process.returncode == 0:
            output = (process.stdout or process.stderr or "").strip()
            if output:
                logging.info(f"uv sync output: {output}")
            return True, "Dependencies synced successfully."
        stderr = (process.stderr or process.stdout or "").strip()
        logging.error(f"uv sync failed: {stderr}")
        return False, f"Dependency sync failed:\n{stderr}"
    except Exception as e:
        logging.error(f"Failed to sync dependencies: {e}")
        return False, f"Dependency sync failed: {e}"


async def verify_startup() -> tuple[bool, str]:
    """Import critical modules in the project venv before restarting."""
    try:
        logging.info("Verifying bot startup imports...")
        process = _run_cmd(
            _uv_cmd("run", "python", "-c", _STARTUP_CHECK),
            check=False,
        )

        if process.returncode == 0:
            return True, "Startup verification passed."

        stderr = (process.stderr or process.stdout or "").strip()
        logging.error(f"Startup verification failed: {stderr}")
        return False, f"Startup verification failed:\n{stderr}"
    except Exception as e:
        logging.error(f"Failed to verify startup: {e}")
        return False, f"Startup verification failed: {e}"


async def rollback_git(revision: str) -> tuple[bool, str]:
    try:
        logging.warning(f"Rolling back git to {revision[:12]}...")
        process = _run_cmd(["git", "reset", "--hard", revision], check=False)
        if process.returncode != 0:
            stderr = (process.stderr or process.stdout or "").strip()
            return False, f"Git rollback failed:\n{stderr}"
        return True, f"Rolled back to {revision[:12]}."
    except Exception as e:
        logging.error(f"Failed to roll back git: {e}")
        return False, f"Git rollback failed: {e}"


async def apply_bot_updates() -> tuple[bool, str]:
    """
    Pull git updates, sync dependencies, verify imports, and roll back on failure.
    Returns (success, message).
    """
    before_revision = await get_git_revision()
    if not before_revision:
        return False, "Could not read current git revision; aborting update."

    pull_ok, pull_message = await pull_updates()
    if not pull_ok:
        return False, pull_message

    sync_ok, sync_message = await sync_dependencies()
    if not sync_ok:
        rollback_ok, rollback_message = await rollback_git(before_revision)
        if rollback_ok:
            await sync_dependencies()
        return False, (
            f"{pull_message}\n{sync_message}\n"
            f"{rollback_message if rollback_ok else 'Rollback also failed; manual fix required.'}"
        )

    verify_ok, verify_message = await verify_startup()
    if not verify_ok:
        rollback_ok, rollback_message = await rollback_git(before_revision)
        if rollback_ok:
            await sync_dependencies()
        return False, (
            f"{pull_message}\n{sync_message}\n{verify_message}\n"
            f"{rollback_message if rollback_ok else 'Rollback also failed; manual fix required.'}"
        )

    return True, f"{pull_message}\n{sync_message}\n{verify_message}"


def restart_bot():
    """
    Restarts the bot by exiting with status 0.
    Assumes a supervisor (like systemd) will restart it.
    """
    import sys

    logging.info("Restarting bot...")
    sys.exit(0)
