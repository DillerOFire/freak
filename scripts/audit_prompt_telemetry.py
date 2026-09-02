#!/usr/bin/env python3
"""Audit one Freak telemetry export and optionally enforce prompt budgets."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bot.telemetry.audit import audit_telemetry_export


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("telemetry", type=Path)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit with status 2 when prompt-budget violations are present",
    )
    args = parser.parse_args()

    with args.telemetry.open("r", encoding="utf-8") as handle:
        document = json.load(handle)
    audit = audit_telemetry_export(document)
    print(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True))
    return 2 if args.check and audit["violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
