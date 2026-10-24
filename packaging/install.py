#!/usr/bin/env python3
"""Standard-library bootstrap entry point shipped in a Silverloupe release."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="Install a verified Silverloupe production release.",
    )
    parser.add_argument(
        "--host",
        action="append",
        choices=("codex", "claude-code"),
        help="host to configure; repeat for both",
    )
    parser.add_argument(
        "--editor",
        choices=(
            "lightroom-classic",
            "lightroom-cloud",
            "photoshop-camera-raw",
            "lightcraft",
            "report-only",
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("native", "file", "report-only"),
        default="report-only",
    )
    parser.add_argument("--install-root", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.host or args.editor is None or args.install_root is None:
        _parser().print_help()
        return 0
    print(
        json.dumps(
            {
                "status": "configuration-required",
                "hosts": args.host,
                "editor": args.editor,
                "mode": args.mode,
                "install_root": str(args.install_root),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
