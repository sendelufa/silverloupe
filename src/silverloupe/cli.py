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
from silverloupe.grouping import group_prepared
from silverloupe.inventory import prepare, resume
from silverloupe.state import read_status
from silverloupe.triage import (
    assess_prepared,
    create_vision_request_prepared,
    import_vision_response_prepared,
)


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
    group_parser = subparsers.add_parser("group", help="group a prepared session")
    group_parser.add_argument("output")
    group_mode = group_parser.add_mutually_exclusive_group()
    group_mode.add_argument(
        "--reuse-groups",
        action="store_true",
        help="validate edited groups.json and rebuild selection items without regrouping",
    )
    group_mode.add_argument(
        "--reset-manual-groups",
        action="store_true",
        help="discard saved manual groups and replace them with automatic grouping",
    )
    assess_parser = subparsers.add_parser(
        "assess", help="measure technical signals and propose review-only triage"
    )
    assess_parser.add_argument("output")
    vision_request_parser = subparsers.add_parser(
        "vision-request", help="write an auditable host-vision request"
    )
    vision_request_parser.add_argument("output")
    vision_import_parser = subparsers.add_parser(
        "vision-import", help="validate and import a host-vision response"
    )
    vision_import_parser.add_argument("output")
    vision_import_parser.add_argument("response")
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
        if args.command == "group":
            output = Path(args.output).expanduser().resolve()
            report = group_prepared(
                output,
                reuse_groups=args.reuse_groups,
                reset_manual_groups=args.reset_manual_groups,
            )
            print(
                json.dumps(
                    {
                        "status": "completed",
                        "assets": report["assets"],
                        "groups": report["groups"],
                        "selection_items": report["selection_items"],
                        "review_groups": len(report["review_group_ids"]),
                        "output": str(output),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "assess":
            output = Path(args.output).expanduser().resolve()
            report = assess_prepared(output)
            print(
                json.dumps(
                    {
                        "status": report["status"],
                        "assets": report["assets"],
                        "vision": report["vision"],
                        "auto_reject_enabled": report["auto_reject_enabled"],
                        "output": str(output),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0 if report["status"] == "completed" else 3
        if args.command == "vision-request":
            output = Path(args.output).expanduser().resolve()
            request = create_vision_request_prepared(output)
            print(
                json.dumps(
                    {
                        "status": "pending",
                        "request_id": request.request_id,
                        "attempt": request.attempt,
                        "assets": len(request.assets),
                        "request": str(output / "vision-request.json"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "vision-import":
            output = Path(args.output).expanduser().resolve()
            response = Path(args.response).expanduser().resolve()
            result = import_vision_response_prepared(output, response)
            print(
                json.dumps(
                    {
                        "status": result.status,
                        "assets": len(result.assessments),
                        "next_attempt": (
                            result.next_request.attempt if result.next_request is not None else None
                        ),
                        "output": str(output),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0 if result.status == "completed" else 3
    except (FileNotFoundError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 4
    return 2
