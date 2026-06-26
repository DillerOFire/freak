#!/usr/bin/env python3
"""Write .build-info.json from git or build-time environment variables."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUILD_INFO_PATH = PROJECT_ROOT / ".build-info.json"


def _from_git() -> dict[str, str]:
    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", *args],
            cwd=PROJECT_ROOT,
            text=True,
        ).strip()

    return {
        "commit": git("rev-parse", "HEAD"),
        "short": git("rev-parse", "--short", "HEAD"),
        "subject": git("log", "-1", "--format=%s"),
        "date": git("log", "-1", "--format=%ci"),
    }


def _from_env() -> dict[str, str]:
    commit = os.environ.get("GIT_COMMIT", "unknown").strip()
    short = os.environ.get("GIT_COMMIT_SHORT", "").strip() or commit[:7]
    return {
        "commit": commit,
        "short": short,
        "subject": os.environ.get("GIT_COMMIT_SUBJECT", "").strip(),
        "date": os.environ.get("GIT_COMMIT_DATE", "").strip(),
    }


def resolve_build_info(from_env: bool = False) -> dict[str, str]:
    if from_env:
        return _from_env()

    try:
        return _from_git()
    except (subprocess.CalledProcessError, FileNotFoundError):
        if os.environ.get("GIT_COMMIT"):
            return _from_env()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-env",
        action="store_true",
        help="Read GIT_COMMIT* variables instead of calling git",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing .build-info.json",
    )
    args = parser.parse_args()

    if BUILD_INFO_PATH.exists() and not args.force:
        return 0

    try:
        info = resolve_build_info(from_env=args.from_env)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Could not resolve build metadata from git or environment.", file=sys.stderr)
        return 1

    BUILD_INFO_PATH.write_text(
        json.dumps(info, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
