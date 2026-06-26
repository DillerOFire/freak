"""Read commit metadata baked into the image or checkout."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUILD_INFO_PATH = PROJECT_ROOT / ".build-info.json"


def load_build_info() -> dict[str, str] | None:
    if BUILD_INFO_PATH.exists():
        try:
            data = json.loads(BUILD_INFO_PATH.read_text(encoding="utf-8"))
            commit = str(data.get("commit", "")).strip()
            if commit and commit != "unknown":
                return {
                    "commit": commit,
                    "short": str(data.get("short") or commit[:7]).strip(),
                    "subject": str(data.get("subject", "")).strip(),
                    "date": str(data.get("date", "")).strip(),
                }
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logging.warning("Failed to read %s: %s", BUILD_INFO_PATH, exc)

    commit = (os.getenv("FREAK_GIT_COMMIT") or os.getenv("GIT_COMMIT") or "").strip()
    if not commit or commit == "unknown":
        return None

    return {
        "commit": commit,
        "short": (
            os.getenv("FREAK_GIT_COMMIT_SHORT")
            or os.getenv("GIT_COMMIT_SHORT")
            or commit[:7]
        ).strip(),
        "subject": (
            os.getenv("FREAK_GIT_COMMIT_SUBJECT") or os.getenv("GIT_COMMIT_SUBJECT") or ""
        ).strip(),
        "date": (
            os.getenv("FREAK_GIT_COMMIT_DATE") or os.getenv("GIT_COMMIT_DATE") or ""
        ).strip(),
    }


def format_build_info(info: dict[str, str]) -> str:
    short = info.get("short") or info["commit"][:7]
    lines = [f"Commit: {short} ({info['commit']})"]
    if info.get("subject"):
        lines.append(f"Message: {info['subject']}")
    if info.get("date"):
        lines.append(f"Date: {info['date']}")
    return "\n".join(lines)
