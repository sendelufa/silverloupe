#!/usr/bin/env python3
"""Standard-library bootstrap entry point shipped in a Silverloupe release."""

from __future__ import annotations

import argparse
import json
import sys
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
    parser.add_argument("--scope", choices=("user", "project"), default="user")
    parser.add_argument(
        "--model-mode",
        choices=("host-vision", "local-signals", "api", "local-vision"),
        default="host-vision",
    )
    parser.add_argument("--cache-quota-gb", type=int, default=5)
    parser.add_argument("--worker-limit", type=int, default=1)
    parser.add_argument("--raw", action="store_true", help="request the optional RAW decoder")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    hosts = args.host
    if not hosts:
        hosts = [input("Host (codex/claude-code): ").strip()]
    editor = args.editor or input("Editor: ").strip()
    install_root = args.install_root
    if install_root is None:
        install_root = Path(input("Install directory: ").strip()).expanduser()

    release_root = Path(__file__).resolve().parent
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(release_root / "runtime/src"))
    from silverloupe.contracts import InstallConfig
    from silverloupe.installer.lifecycle import install_release

    manifest = json.loads((release_root / "manifest.json").read_text(encoding="utf-8"))
    config = InstallConfig(
        hosts=hosts,
        scope=args.scope,
        install_root=str(install_root.resolve()),
        enabled_editors=[editor],
        default_editor=editor,
        integration_mode_by_editor={editor: args.mode},
        runtime_version=manifest["release_version"],
        model_mode=args.model_mode,
        model_profile={"source": "release-installer"},
        cache_quota=args.cache_quota_gb * 1024**3,
        worker_limit=args.worker_limit,
        install_raw_extra=args.raw,
    )
    result = install_release(release_root, config)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
