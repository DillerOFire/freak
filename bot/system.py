import logging
import os
import shutil
import subprocess
from functools import lru_cache

from bot.build_info import format_build_info, load_build_info

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


YTDLP_GIT_SOURCE = "git+https://github.com/yt-dlp/yt-dlp.git@master"


def _ytdlp_output_indicates_update(output: str) -> bool:
    lowered = output.lower()
    return any(
        token in lowered
        for token in ("installed", "upgraded", "updated", "built", "prepared")
    )


def _process_text(value: str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _format_ytdlp_result(process: subprocess.CompletedProcess[str], *, location: str) -> tuple[bool, str]:
    stdout = _process_text(process.stdout)
    stderr = _process_text(process.stderr)
    combined = "\n".join(part for part in (stdout, stderr) if part)

    if process.returncode == 0:
        logging.info("yt-dlp update output (%s): %s", location, combined)
        if _ytdlp_output_indicates_update(combined):
            return True, f"yt-dlp updated successfully ({location}):\n{combined}"
        return True, "yt-dlp is already up to date."

    logging.error("yt-dlp update failed (%s): %s", location, combined)
    return False, f"yt-dlp update failed ({location}):\n{combined}"


async def update_ytdlp_package() -> tuple[bool, str]:
    """
    Update yt-dlp in the project venv, falling back to a writable package dir
    when the venv is not writable (common in Docker images built as root).
    Returns (success, message).
    """
    from config import YTDLP_PACKAGE_DIR

    try:
        logging.info("Attempting to update yt-dlp in project venv...")
        process = _run_cmd(
            _uv_cmd("pip", "install", "-U", YTDLP_GIT_SOURCE),
            check=False,
        )
        success, message = _format_ytdlp_result(process, location="venv")
        if success:
            return success, message

        combined = message.lower()
        if "permission denied" not in combined and "read-only" not in combined:
            return False, message

        os.makedirs(YTDLP_PACKAGE_DIR, exist_ok=True)
        logging.info(
            "Venv yt-dlp update is not writable; installing to %s",
            YTDLP_PACKAGE_DIR,
        )
        fallback = _run_cmd(
            _uv_cmd(
                "pip",
                "install",
                "-U",
                "--target",
                YTDLP_PACKAGE_DIR,
                YTDLP_GIT_SOURCE,
            ),
            check=False,
        )
        return _format_ytdlp_result(fallback, location=YTDLP_PACKAGE_DIR)

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


async def get_version_info() -> str:
    """Return a human-readable version string for the running checkout."""
    build_info = load_build_info()
    if build_info:
        return format_build_info(build_info)

    try:
        process = _run_cmd(
            ["git", "log", "-1", "--format=%H%n%h%n%s%n%ci"],
            check=True,
        )
        lines = [line.strip() for line in process.stdout.splitlines() if line.strip()]
        if len(lines) >= 4:
            full_hash, short_hash, subject, committed_at = lines[:4]
            return format_build_info(
                {
                    "commit": full_hash,
                    "short": short_hash,
                    "subject": subject,
                    "date": committed_at,
                }
            )
    except Exception as e:
        logging.error(f"Failed to read git version info: {e}")

    revision = await get_git_revision()
    if revision:
        return format_build_info(
            {"commit": revision, "short": revision[:7], "subject": "", "date": ""}
        )
    return "Version unknown (not a git checkout)."


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
