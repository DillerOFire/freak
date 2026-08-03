import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
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


def _run_cmd(
    args: list[str],
    *,
    check: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=check,
        cwd=PROJECT_ROOT,
        env=env,
    )


YTDLP_GIT_SOURCE = "git+https://github.com/yt-dlp/yt-dlp.git@master"
_ytdlp_update_lock = asyncio.Lock()


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


def _atomic_symlink(target: str, link_path: str) -> None:
    """Atomically replace a symlink without exposing a half-written target."""
    parent = os.path.dirname(link_path)
    relative_target = os.path.relpath(target, parent)
    temporary_link = os.path.join(parent, f".{os.path.basename(link_path)}-{time.time_ns()}")
    os.symlink(relative_target, temporary_link)
    try:
        os.replace(temporary_link, link_path)
    except Exception:
        try:
            os.unlink(temporary_link)
        except OSError:
            pass
        raise


def _verify_ytdlp_target(target: str) -> tuple[bool, str]:
    """Import a staged build in a fresh interpreter and return its version."""
    if not os.path.isfile(os.path.join(target, "yt_dlp", "__init__.py")):
        return False, "staged target does not contain yt_dlp"
    verify_code = (
        "import sys; "
        f"sys.path.insert(0, {target!r}); "
        "import yt_dlp; print(yt_dlp.version.__version__)"
    )
    process = _run_cmd([sys.executable, "-c", verify_code], check=False)
    if process.returncode != 0:
        detail = _process_text(process.stderr) or _process_text(process.stdout)
        return False, detail or "staged yt-dlp import failed"
    version = _process_text(process.stdout).splitlines()[-1].strip()
    if not version:
        return False, "staged yt-dlp did not report a version"
    return True, version


def _active_ytdlp_version() -> str:
    try:
        import yt_dlp

        return str(yt_dlp.version.__version__)
    except Exception:
        return "unknown"


def _active_ytdlp_target() -> str | None:
    """Return the site-packages root that supplied the running yt-dlp."""
    try:
        import yt_dlp

        return str(os.path.dirname(os.path.dirname(os.path.realpath(yt_dlp.__file__))))
    except Exception:
        return None


def _prune_ytdlp_releases(package_root: str, keep: set[str]) -> None:
    releases_dir = os.path.join(package_root, "releases")
    candidates = []
    for name in os.listdir(releases_dir):
        path = os.path.join(releases_dir, name)
        if os.path.isdir(path) and path not in keep and not name.startswith(".staging-"):
            candidates.append(path)
    candidates.sort(key=os.path.getmtime, reverse=True)
    for stale in candidates[1:]:
        shutil.rmtree(stale, ignore_errors=True)


def _install_staged_ytdlp(package_root: str) -> tuple[bool, str]:
    """Install nightly yt-dlp, verify it, and atomically activate it."""
    releases_dir = os.path.join(package_root, "releases")
    os.makedirs(releases_dir, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".staging-", dir=releases_dir)
    try:
        logging.info("Staging nightly yt-dlp update in %s", staging)
        process = _run_cmd(
            _uv_cmd(
                "pip",
                "install",
                "-U",
                "--target",
                staging,
                YTDLP_GIT_SOURCE,
            ),
            check=False,
        )
        if process.returncode != 0:
            return _format_ytdlp_result(process, location="staging")

        verified, version_or_error = _verify_ytdlp_target(staging)
        if not verified:
            logging.error("Staged yt-dlp verification failed: %s", version_or_error)
            return False, f"yt-dlp staged verification failed: {version_or_error}"

        version = version_or_error
        current_version = _active_ytdlp_version()
        current_link = os.path.join(package_root, "current")
        if version == current_version and os.path.islink(current_link):
            return True, f"yt-dlp nightly {version} is already active."

        safe_version = re.sub(r"[^A-Za-z0-9._-]", "_", version)
        release = os.path.join(releases_dir, f"{safe_version}-{time.time_ns()}")
        os.replace(staging, release)
        staging = ""

        previous_target = _active_ytdlp_target()
        if os.path.islink(current_link):
            previous_target = os.path.realpath(current_link)
        if previous_target and os.path.isdir(previous_target):
            _atomic_symlink(previous_target, os.path.join(package_root, "previous"))

        _atomic_symlink(release, current_link)
        keep = {release}
        if previous_target and os.path.commonpath((releases_dir, previous_target)) == releases_dir:
            keep.add(previous_target)
        _prune_ytdlp_releases(package_root, keep)
        logging.info(
            "Activated yt-dlp nightly version=%s previous=%s path=%s",
            version,
            current_version,
            release,
        )
        return True, (
            f"yt-dlp updated successfully (staged nightly): {current_version} -> {version}. "
            "The previous build was retained for rollback."
        )
    except Exception as exc:
        logging.exception("Staged yt-dlp update failed")
        return False, f"yt-dlp staged update failed: {exc}"
    finally:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)


async def update_ytdlp_package() -> tuple[bool, str]:
    """Serialize and stage a nightly yt-dlp update outside the event loop."""
    from config import YTDLP_PACKAGE_DIR

    async with _ytdlp_update_lock:
        return await asyncio.to_thread(_install_staged_ytdlp, YTDLP_PACKAGE_DIR)


def _rollback_staged_ytdlp(package_root: str) -> tuple[bool, str]:
    current_link = os.path.join(package_root, "current")
    previous_link = os.path.join(package_root, "previous")
    if not os.path.islink(previous_link):
        return False, "No previous staged yt-dlp build is available."
    previous_target = os.path.realpath(previous_link)
    verified, previous_version = _verify_ytdlp_target(previous_target)
    if not verified:
        return False, f"Previous yt-dlp build failed verification: {previous_version}"

    current_target = os.path.realpath(current_link) if os.path.islink(current_link) else None
    _atomic_symlink(previous_target, current_link)
    if current_target and os.path.isdir(current_target):
        _atomic_symlink(current_target, previous_link)
    return True, f"yt-dlp rolled back successfully to {previous_version}."


async def rollback_ytdlp_package() -> tuple[bool, str]:
    """Atomically reactivate the previous verified nightly build."""
    from config import YTDLP_PACKAGE_DIR

    async with _ytdlp_update_lock:
        return await asyncio.to_thread(_rollback_staged_ytdlp, YTDLP_PACKAGE_DIR)


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
