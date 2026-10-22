"""Command-line entry points for Silverloupe."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any, Sequence

from silverloupe import __version__
from silverloupe.contracts import SCHEMA_VERSION, Brief, RunConfig
from silverloupe.inventory import prepare, resume
from silverloupe.state import read_status


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
    prepare_parser = subparsers.add_parser("prepare", help="inventory images and create previews")
    prepare_parser.add_argument("source")
    prepare_parser.add_argument("output")
    prepare_parser.add_argument("--max-edge", type=int, default=1600)
    status_parser = subparsers.add_parser("status", help="show persisted preparation status")
    status_parser.add_argument("output")
    resume_parser = subparsers.add_parser("resume", help="resume preparation from checkpoints")
    resume_parser.add_argument("output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "doctor":
            print(json.dumps(doctor_report(), ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "prepare":
            source = Path(args.source).expanduser().resolve()
            output = Path(args.output).expanduser().resolve()
            config = RunConfig(
                source_roots=[str(source)],
                output_root=str(output),
                brief=Brief(),
                rendering_policy={"max_edge": args.max_edge},
            )
            manifest = prepare(source, output, config)
            unavailable = sum(asset.decode_status != "ready" for asset in manifest.assets)
            print(
                json.dumps(
                    {
                        "status": "completed",
                        "assets": len(manifest.assets),
                        "unavailable": unavailable,
                        "output": str(output),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "status":
            print(json.dumps(read_status(Path(args.output)), ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "resume":
            output = Path(args.output).expanduser().resolve()
            manifest = resume(output)
            print(
                json.dumps(
                    {
                        "status": "completed",
                        "assets": len(manifest.assets),
                        "unavailable": sum(asset.decode_status != "ready" for asset in manifest.assets),
                        "output": str(output),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
    except (FileNotFoundError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 4
    return 2
