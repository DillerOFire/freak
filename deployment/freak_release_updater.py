#!/usr/bin/env python3
"""Reconcile Freak and PersonFreak to the newest public verified release.

This is deliberately host-local: it reads public GitHub release assets, pulls
an immutable GHCR image, and invokes Docker Compose for the two existing bot
projects. It never clones or reads the infrastructure repository.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

API_URL = "https://api.github.com/repos/DillerOFire/freak/releases"
TAG = re.compile(r"^continuous-([0-9a-f]{40})$")
IMAGE = re.compile(r"^ghcr\.io/dillerofire/freak@sha256:([0-9a-f]{64})$")
RELEASE_ID = re.compile(r"^[0-9a-f]{64}$")


class NoVerifiedReleaseError(ValueError):
    """No master build has completed its immutable promotion yet."""


@dataclass(frozen=True)
class Target:
    name: str
    container: str


TARGETS = (
    Target("freak", "freak"),
    Target("personfreak", "personfreak"),
)


def run(*command: str, capture: bool = False) -> str:
    result = subprocess.run(command, check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else ""


def request_json(url: str) -> object:
    request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "freak-release-updater"})
    with urlopen(request, timeout=30) as response:  # nosec B310: fixed public GitHub API/asset URLs
        return json.load(response)


def download_text(url: str) -> str:
    request = Request(url, headers={"Accept": "application/octet-stream", "User-Agent": "freak-release-updater"})
    with urlopen(request, timeout=30) as response:  # nosec B310: URLs come from the verified GitHub release
        return response.read().decode("utf-8").strip()


def latest_release() -> tuple[str, str]:
    releases = request_json(API_URL)
    if not isinstance(releases, list):
        raise ValueError("GitHub releases response is not a list")
    candidates: list[tuple[datetime, str, str]] = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            continue
        tag = release.get("tag_name")
        published_at = release.get("published_at")
        if not isinstance(tag, str) or not TAG.fullmatch(tag) or not isinstance(published_at, str):
            continue
        try:
            published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        assets = release.get("assets")
        if not isinstance(assets, list):
            continue
        urls = {
            asset.get("name"): asset.get("browser_download_url")
            for asset in assets
            if isinstance(asset, dict)
            and isinstance(asset.get("name"), str)
            and isinstance(asset.get("browser_download_url"), str)
        }
        image_file, identifier_file = urls.get("image.env"), urls.get("image.env.release-id")
        if not image_file or not identifier_file:
            continue
        image_line = download_text(image_file)
        identifier = download_text(identifier_file)
        if not image_line.startswith("FREAK_IMAGE="):
            raise ValueError(f"{tag} image.env has no FREAK_IMAGE")
        image = image_line.removeprefix("FREAK_IMAGE=")
        image_match = IMAGE.fullmatch(image)
        if not image_match or not RELEASE_ID.fullmatch(identifier):
            raise ValueError(f"{tag} has invalid immutable image metadata")
        if image_match.group(1) != identifier:
            raise ValueError(f"{tag} image digest does not match its release id")
        candidates.append((published, tag, image))
    if candidates:
        _, tag, image = max(candidates)
        return tag, image
    raise NoVerifiedReleaseError("no verified continuous release is available")


def load_state(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"invalid updater state at {path}")
    return value


def save_state(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
        json.dump(value, temporary, sort_keys=True)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    temporary_path.chmod(0o600)
    temporary_path.replace(path)


def current_image(target: Target) -> str:
    image = run("docker", "inspect", "--format", "{{.Config.Image}}", target.container, capture=True)
    if not IMAGE.fullmatch(image):
        raise ValueError(f"{target.name} is not running a Freak immutable image")
    return image


def compose_project(target: Target) -> tuple[str, Path]:
    """Read the running project's own Compose metadata, not an external checkout."""
    raw_labels = run("docker", "inspect", "--format", "{{json .Config.Labels}}", target.container, capture=True)
    try:
        labels = json.loads(raw_labels)
        project = labels["com.docker.compose.project"]
        files = [Path(value) for value in labels["com.docker.compose.project.config_files"].split(",")]
        working_dir = Path(labels["com.docker.compose.project.working_dir"])
    except (json.JSONDecodeError, KeyError, AttributeError) as error:
        raise ValueError(f"{target.name} is not managed by one Compose project") from error
    base_files = [path for path in files if path.parent == working_dir]
    if not isinstance(project, str) or len(base_files) != 1:
        raise ValueError(f"{target.name} does not have one usable base Compose source")
    compose = base_files[0]
    if not compose.is_file():
        raise ValueError(f"{target.name} Compose metadata does not describe its live project")
    return project, compose


def override_path(overrides: Path, target: Target) -> Path:
    return overrides / f"{target.name}.compose.yml"


def write_override(path: Path, image: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(f"services:\n  bot:\n    image: {image}\n", encoding="utf-8")
    path.chmod(0o600)


def apply(target: Target, override: Path) -> None:
    project, compose = compose_project(target)
    run("docker", "compose", "--project-name", project, "--file", str(compose), "--file", str(override), "up", "--detach", "--no-deps", "--pull", "never", "bot")


def wait_healthy(target: Target, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = run("docker", "inspect", "--format", "{{.State.Health.Status}}", target.container, capture=True)
        if status == "healthy":
            return
        if status == "unhealthy":
            break
        time.sleep(2)
    raise RuntimeError(f"{target.name} did not become healthy")


def deploy(image: str, overrides: Path, health_timeout: int) -> dict[str, str]:
    previous = {target.name: current_image(target) for target in TARGETS}
    run("docker", "pull", image)
    try:
        for target in TARGETS:
            path = override_path(overrides, target)
            write_override(path, image)
            apply(target, path)
        for target in TARGETS:
            wait_healthy(target, health_timeout)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError):
        for target in TARGETS:
            path = override_path(overrides, target)
            write_override(path, previous[target.name])
            try:
                apply(target, path)
            except (OSError, ValueError, subprocess.CalledProcessError) as rollback_error:
                print(f"rollback failed for {target.name}: {rollback_error}", file=sys.stderr)
        raise
    return previous


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path("/var/lib/freak-release-updater/state.json"))
    parser.add_argument("--overrides", type=Path, default=Path("/var/lib/freak-release-updater/compose"))
    parser.add_argument("--health-timeout", type=int, default=120)
    parser.add_argument("--check", action="store_true", help="show the desired release without changing containers")
    args = parser.parse_args()
    if args.health_timeout < 1:
        parser.error("--health-timeout must be positive")

    try:
        tag, image = latest_release()
    except NoVerifiedReleaseError as error:
        print(error)
        return 0
    if args.check:
        print(f"{tag} {image}")
        return 0
    state = load_state(args.state)
    if state.get("image") == image and all(current_image(target) == image for target in TARGETS):
        print(f"already reconciled {tag} ({image})")
        return 0
    try:
        previous = deploy(image, args.overrides, args.health_timeout)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        state.update({"failed_image": image, "failed_tag": tag, "last_error": str(error)})
        save_state(args.state, state)
        raise
    state.update({"image": image, "tag": tag, "previous": previous, "last_error": ""})
    save_state(args.state, state)
    print(f"reconciled {tag} ({image})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
