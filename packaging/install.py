#!/usr/bin/env python3
"""Standard-library bootstrap entry point shipped in a Silverloupe release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Sequence


_VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9A-Za-z-]+)*$")


def _verify_release_root(release_root: Path) -> dict[str, object]:
    """Verify every release byte before importing the bundled runtime."""

    if release_root.is_symlink() or not release_root.is_dir():
        raise ValueError("release root is missing or unsafe")
    files: dict[str, bytes] = {}
    for candidate in sorted(release_root.rglob("*")):
        if candidate.is_symlink():
            raise ValueError(f"release contains a symlink: {candidate}")
        if candidate.is_file():
            relative = candidate.relative_to(release_root).as_posix()
            if ".." in Path(relative).parts:
                raise ValueError(f"unsafe release path: {relative}")
            files[relative] = candidate.read_bytes()
    try:
        manifest = json.loads(files["manifest.json"])
        checksums = json.loads(files["checksums.json"])
    except KeyError as error:
        raise ValueError(f"release metadata is missing: {error.args[0]}") from error
    except (TypeError, ValueError) as error:
        raise ValueError(f"release metadata is invalid: {error}") from error
    if not isinstance(manifest, dict) or not isinstance(checksums, dict):
        raise ValueError("release metadata must contain JSON objects")
    ownership = manifest.get("ownership")
    if not isinstance(ownership, list) or set(ownership) != set(files):
        raise ValueError("release ownership does not exactly match artifact files")
    payload = set(files) - {"manifest.json", "checksums.json"}
    if set(checksums) != payload:
        raise ValueError("release checksums do not exactly cover payload files")
    for relative, expected in checksums.items():
        actual = hashlib.sha256(files[relative].encode() if isinstance(files[relative], str) else files[relative]).hexdigest()
        if not isinstance(expected, str) or actual != expected:
            raise ValueError(f"checksum mismatch for {relative}")
    version = manifest.get("release_version")
    if not isinstance(version, str) or not _VERSION_PATTERN.fullmatch(version):
        raise ValueError("release manifest contains an invalid version")
    return manifest


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
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="fail instead of prompting when required choices are missing",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    discovery: Any = None,
    io: Any = None,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    release_root = Path(__file__).resolve().parent
    sys.dont_write_bytecode = True
    try:
        manifest = _verify_release_root(release_root)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    sys.path.insert(0, str(release_root / "runtime/src"))
    from silverloupe.contracts import InstallConfig
    from silverloupe.installer.capabilities import probe_install
    from silverloupe.installer.discovery import (
        default_install_root,
        discover_install_environment,
    )
    from silverloupe.installer.lifecycle import install_release
    from silverloupe.installer.presentation import (
        CursesWizardIO,
        InstallerCancelled,
    )
    from silverloupe.installer.wizard import (
        InstallerPrerequisiteError,
        run_installer_wizard,
    )

    fully_specified = bool(args.host and args.editor and args.install_root)
    partially_specified = bool(
        args.host
        or args.editor
        or args.install_root
        or args.scope != "user"
        or args.mode != "report-only"
        or args.model_mode != "host-vision"
        or args.cache_quota_gb != 5
        or args.worker_limit != 1
        or args.raw
    )
    if not fully_specified and (args.non_interactive or partially_specified):
        parser.error(
            "--host, --editor and --install-root must be supplied together "
            "for a non-interactive installation"
        )
    if fully_specified:
        editor = args.editor
        assert editor is not None and args.install_root is not None
        model_profile = {"source": "release-installer"}
        uv_executable = os.environ.get("SILVERLOUPE_UV_EXECUTABLE")
        if uv_executable:
            model_profile["uv_executable"] = uv_executable
        config = InstallConfig(
            hosts=args.host,
            scope=args.scope,
            install_root=str(args.install_root.expanduser().resolve()),
            enabled_editors=[editor],
            default_editor=editor,
            integration_mode_by_editor={editor: args.mode},
            runtime_version=manifest["release_version"],
            model_mode=args.model_mode,
            model_profile=model_profile,
            cache_quota=args.cache_quota_gb * 1024**3,
            worker_limit=args.worker_limit,
            install_raw_extra=args.raw,
        )
        result = install_release(release_root, config)
        result["capabilities"] = probe_install(config)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    if discovery is None:
        provider: Callable[[], Any] = lambda: discover_install_environment(
            default_install_root(Path.home())
        )
    elif callable(discovery):
        provider = discovery
    else:
        provider = lambda: discovery
    adapter = io or CursesWizardIO()
    try:
        result = run_installer_wizard(
            release_root,
            manifest,
            provider(),
            adapter,
            rediscover=provider,
        )
    except InstallerCancelled:
        return 0
    except InstallerPrerequisiteError as error:
        adapter.show_result(
            "Cannot install Silverloupe",
            [("Prerequisite", "unavailable", str(error))],
        )
        return 2
    return 0 if result.get("status") != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
