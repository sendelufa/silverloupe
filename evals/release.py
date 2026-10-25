"""Fail-closed qualification of an exact Silverloupe production artifact."""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any


_FORBIDDEN_PARTS = {
    ".pytest_cache",
    "__pycache__",
    "dev",
    "evals",
    "gold",
    "tests",
}
_FORBIDDEN_DEPENDENCIES = ("pytest", "jsonschema")
_ARCHIVE_NAME = re.compile(r"silverloupe-(?P<version>[^/]+)\.tar\.gz\Z")


def _json_bytes(value: bytes, name: str, errors: list[str]) -> Any | None:
    try:
        return json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        errors.append(f"{name} is invalid JSON: {error}")
        return None


def _inspect_archive(artifact: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    errors: list[str] = []
    forbidden_members: list[str] = []
    checksum_mismatches: list[str] = []
    files: dict[str, bytes] = {}
    root_names: set[str] = set()
    try:
        with tarfile.open(artifact, "r:gz") as archive:
            for member in archive.getmembers():
                path = PurePosixPath(member.name)
                parts = path.parts
                if path.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
                    errors.append(f"unsafe archive member: {member.name}")
                    continue
                root_names.add(parts[0])
                if member.issym() or member.islnk():
                    errors.append(f"archive links are forbidden: {member.name}")
                    continue
                if member.isdir():
                    continue
                if not member.isfile():
                    errors.append(f"unsupported archive member type: {member.name}")
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    errors.append(f"could not read archive member: {member.name}")
                    continue
                if len(parts) < 2:
                    errors.append(f"archive file is outside the release root: {member.name}")
                    continue
                relative = PurePosixPath(*parts[1:]).as_posix()
                if relative in files:
                    errors.append(f"duplicate archive member: {relative}")
                    continue
                files[relative] = extracted.read()
                if _FORBIDDEN_PARTS.intersection(parts[1:]):
                    forbidden_members.append(relative)
    except (OSError, tarfile.TarError) as error:
        errors.append(f"could not read release archive: {error}")

    if len(root_names) != 1:
        errors.append("release archive must contain exactly one root directory")
    manifest = _json_bytes(files.get("manifest.json", b""), "manifest.json", errors)
    checksums = _json_bytes(files.get("checksums.json", b""), "checksums.json", errors)
    if not isinstance(manifest, dict):
        manifest = {}
    if not isinstance(checksums, dict) or not all(
        isinstance(name, str) and isinstance(digest, str)
        for name, digest in checksums.items()
    ):
        errors.append("checksums.json must be a string-to-string object")
        checksums = {}

    payload_names = set(files) - {"manifest.json", "checksums.json"}
    if set(checksums) != payload_names:
        errors.append("checksums.json does not cover the exact release payload")
    for name, expected in checksums.items():
        data = files.get(name)
        if data is None or hashlib.sha256(data).hexdigest() != expected:
            checksum_mismatches.append(name)

    ownership = manifest.get("ownership")
    if not isinstance(ownership, list) or set(ownership) != set(files):
        errors.append("manifest ownership does not cover the exact release files")
    release_version = manifest.get("release_version")
    filename_match = _ARCHIVE_NAME.fullmatch(artifact.name)
    filename_version = filename_match.group("version") if filename_match else None
    if not isinstance(release_version, str) or filename_version != release_version:
        errors.append(
            f"filename version {filename_version!r} does not match manifest release version "
            f"{release_version!r}"
        )
    if root_names and isinstance(release_version, str):
        expected_root = f"silverloupe-{release_version}"
        if root_names != {expected_root}:
            errors.append("archive root does not match the manifest release version")

    dependency_text = b"\n".join(
        files.get(name, b"")
        for name in (
            "runtime/requirements.lock",
            "runtime/requirements-raw.lock",
            "runtime/pyproject.toml",
        )
    ).decode("utf-8", errors="replace").lower()
    for dependency in _FORBIDDEN_DEPENDENCIES:
        if dependency in dependency_text:
            errors.append(f"forbidden development dependency is present: {dependency}")

    archive_result = {
        "valid": not errors and not forbidden_members and not checksum_mismatches,
        "root": next(iter(root_names)) if len(root_names) == 1 else None,
        "file_count": len(files),
        "forbidden_members": sorted(forbidden_members),
        "checksum_mismatches": sorted(checksum_mismatches),
        "errors": errors,
    }
    return archive_result, manifest


def _dataset_evidence(source_root: Path) -> tuple[dict[str, Any], list[str]]:
    path = source_root / "evals/dataset-manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        datasets = value["datasets"]
        if not isinstance(datasets, list):
            raise TypeError("datasets must be an array")
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        return {
            "available": 0,
            "unavailable": 0,
            "unavailable_ids": [],
            "error": str(error),
        }, ["dataset manifest is unavailable or invalid"]
    unavailable_ids = sorted(
        str(dataset.get("dataset_id"))
        for dataset in datasets
        if isinstance(dataset, dict) and dataset.get("availability") != "available"
    )
    result = {
        "available": len(datasets) - len(unavailable_ids),
        "unavailable": len(unavailable_ids),
        "unavailable_ids": unavailable_ids,
    }
    return result, (["real photo datasets are unavailable"] if unavailable_ids else [])


def _editor_evidence(source_root: Path) -> tuple[dict[str, Any], list[str]]:
    path = source_root / "evals/compatibility-matrix.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        combinations = value["combinations"]
        if not isinstance(combinations, list):
            raise TypeError("combinations must be an array")
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        return {"statuses": {}, "total": 0, "error": str(error)}, [
            "editor compatibility matrix is unavailable or invalid"
        ]
    statuses = Counter(
        str(combination.get("status", "missing"))
        for combination in combinations
        if isinstance(combination, dict)
    )
    result = {"statuses": dict(sorted(statuses.items())), "total": len(combinations)}
    blockers = [] if combinations and statuses == {"verified": len(combinations)} else [
        "editor round-trip combinations remain unverified"
    ]
    return result, blockers


def _trial_evidence(evidence_root: Path | None) -> tuple[dict[str, Any], list[str], bool]:
    if evidence_root is None:
        return {"status": "unavailable", "reason": "evidence root was not supplied"}, [
            "live release trial evidence is unavailable"
        ], False
    summary_path = evidence_root / "summary.json"
    if not summary_path.is_file():
        return {"status": "unavailable", "reason": "summary.json was not found"}, [
            "live release trial evidence is unavailable"
        ], False
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        statuses = summary["statuses"]
        trials = int(summary["trials"])
        failed = int(statuses.get("failed", 0))
        partial = int(statuses.get("partial", 0))
        passed = int(statuses.get("passed", 0))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        return {"status": "invalid", "reason": str(error)}, [
            "release trial summary is invalid"
        ], True
    if failed:
        status = "failed"
        blockers = ["one or more release trials failed"]
        hard_blocker = True
    elif partial or passed != trials or trials == 0:
        status = "partial" if trials else "unavailable"
        blockers = ["release trials are incomplete"]
        hard_blocker = False
    else:
        status = "passed"
        blockers = []
        hard_blocker = False
    return {"status": status, "summary": summary}, blockers, hard_blocker


def qualify_release(
    artifact: Path,
    *,
    source_root: Path,
    evidence_root: Path | None = None,
) -> dict[str, Any]:
    """Qualify an exact archive without upgrading absent evidence to success."""

    artifact = artifact.expanduser().resolve(strict=True)
    source_root = source_root.expanduser().resolve(strict=True)
    archive, manifest = _inspect_archive(artifact)
    datasets, dataset_blockers = _dataset_evidence(source_root)
    editor, editor_blockers = _editor_evidence(source_root)
    trials, trial_blockers, trial_hard_blocker = _trial_evidence(evidence_root)
    blockers = [*dataset_blockers, *editor_blockers, *trial_blockers]
    hard_blocker = not archive["valid"] or trial_hard_blocker

    gates = trials.get("summary", {}).get("release_gates", {})
    upper_bound = gates.get("false_reject_upper_bound")
    protected_failures = gates.get("protected_case_failures")
    reliability = gates.get("runtime_reliability")
    auto_reject_enabled = bool(
        not hard_blocker
        and datasets["unavailable"] == 0
        and isinstance(upper_bound, (int, float))
        and not isinstance(upper_bound, bool)
        and upper_bound <= 0.005
        and protected_failures == 0
        and reliability == 1.0
    )
    eligible = bool(
        auto_reject_enabled
        and editor["total"] > 0
        and editor["statuses"] == {"verified": editor["total"]}
        and trials["status"] == "passed"
    )
    decision = "blocked" if hard_blocker else ("eligible" if eligible else "recommendation-only")
    return {
        "schema_version": 1,
        "artifact": {
            "path": str(artifact),
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "size_bytes": artifact.stat().st_size,
            "release_version": manifest.get("release_version"),
            "core_version": manifest.get("core_version"),
            "skills_version": manifest.get("skills_version"),
        },
        "archive": archive,
        "datasets": datasets,
        "editor_compatibility": editor,
        "trial_evidence": trials,
        "decision": decision,
        "auto_reject_enabled": auto_reject_enabled,
        "blockers": blockers,
    }
