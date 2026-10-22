"""Command-line entry points for Silverloupe."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import sys
from typing import Any, Sequence

from silverloupe import __version__
from silverloupe.contracts import SCHEMA_VERSION


def doctor_report() -> dict[str, Any]:
    """Inspect optional capabilities without importing or modifying user libraries."""

    python_ready = sys.version_info[:2] == (3, 12)
    exiftool_path = shutil.which("exiftool")
    raw_available = importlib.util.find_spec("rawpy") is not None
    vision_value = os.environ.get("SILVERLOUPE_VISION_CAPABILITY", "unavailable").strip().lower()
    vision_status = vision_value if vision_value in {"ready", "degraded", "unavailable"} else "unavailable"

    return {
        "python": {
            "status": "ready" if python_ready else "degraded",
            "version": platform.python_version(),
            "required": ">=3.12,<3.13",
        },
        "core": {"status": "ready", "version": __version__},
        "schema": {"status": "ready", "version": SCHEMA_VERSION},
        "exiftool": (
            {"status": "ready", "path": exiftool_path}
            if exiftool_path
            else {"status": "unavailable", "reason": "exiftool was not found on PATH"}
        ),
        "raw": (
            {"status": "ready", "backend": "rawpy"}
            if raw_available
            else {"status": "unavailable", "reason": "install the raw extra or use a verified editor renderer"}
        ),
        "vision": {
            "status": vision_status,
            "mode": "host-vision",
            **({"reason": "host vision capability was not declared"} if vision_status == "unavailable" else {}),
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="silverloupe")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="report runtime and optional capabilities")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "doctor":
        print(json.dumps(doctor_report(), ensure_ascii=False, sort_keys=True))
        return 0
    return 2
