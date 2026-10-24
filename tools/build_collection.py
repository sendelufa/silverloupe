"""Build closed, deterministic Silverloupe skill collections."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import shutil
from pathlib import Path
from typing import Any, Literal


PRODUCTION_SKILLS = (
    "silverloupe",
    "silverloupe-prepare",
    "silverloupe-triage",
    "silverloupe-compare",
    "silverloupe-curate",
    "silverloupe-deliver",
)
_LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
_RUNTIME_REFERENCES = {
    "../../resources/contracts.md": "references/contracts.md",
    "../../resources/artistic-intent.md": "references/artistic-intent.md",
    "../../resources/genres/wedding.md": "references/genres/wedding.md",
    "../../resources/genres/architecture.md": "references/genres/architecture.md",
    "../../resources/genres/landscape.md": "references/genres/landscape.md",
}
_DEV_REFERENCES = {
    "../../../evals/cases/routing.jsonl": "references/routing.jsonl",
    "../../../evals/baselines/issue-6-b2.jsonl": "references/issue-6-b2.jsonl",
}


def _safe_source(path: Path, root: Path) -> Path:
    if path.is_symlink():
        raise ValueError(f"source allowlist entry is a symlink: {path}")
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"source allowlist entry escapes source root: {path}")
    if not path.is_file():
        raise FileNotFoundError(f"required collection source does not exist: {path}")
    return path


def _prepare_destination(destination: Path) -> None:
    if destination.is_symlink():
        raise ValueError(f"destination must not be a symlink: {destination}")
    if destination.exists():
        if not destination.is_dir():
            raise ValueError(f"destination must be a directory: {destination}")
        if any(destination.iterdir()):
            raise ValueError(f"destination must be empty: {destination}")
    else:
        destination.mkdir(parents=True)


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _validate_and_rewrite(
    document: str,
    replacements: dict[str, str],
    *,
    source_path: Path,
    source_root: Path,
) -> tuple[str, dict[str, Path]]:
    copies: dict[str, Path] = {}
    for link in _LINK_PATTERN.findall(document):
        if link not in replacements:
            raise ValueError(f"unsupported or escaping reference {link!r} in {source_path}")
        source_reference = _safe_source(source_path.parent / link, source_root)
        copies[replacements[link]] = source_reference
    rewritten = document
    for source_link, bundle_link in replacements.items():
        rewritten = rewritten.replace(f"]({source_link})", f"]({bundle_link})")
    return rewritten, copies


def _runtime_files(source_root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for skill_name in PRODUCTION_SKILLS:
        source_path = _safe_source(
            source_root / "skills" / skill_name / "SKILL.md", source_root
        )
        document = source_path.read_text(encoding="utf-8")
        rewritten, references = _validate_and_rewrite(
            document,
            _RUNTIME_REFERENCES,
            source_path=source_path,
            source_root=source_root,
        )
        prefix = f"skills/{skill_name}"
        files[f"{prefix}/SKILL.md"] = rewritten.encode("utf-8")
        for relative, reference in references.items():
            files[f"{prefix}/{relative}"] = reference.read_bytes()
    return files


def _dev_files(source_root: Path) -> dict[str, bytes]:
    source_path = _safe_source(
        source_root / "dev/skills/silverloupe-eval/SKILL.md", source_root
    )
    document = source_path.read_text(encoding="utf-8")
    rewritten, references = _validate_and_rewrite(
        document,
        _DEV_REFERENCES,
        source_path=source_path,
        source_root=source_root,
    )
    prefix = "skills/silverloupe-eval"
    files = {f"{prefix}/SKILL.md": rewritten.encode("utf-8")}
    for relative, reference in references.items():
        files[f"{prefix}/{relative}"] = reference.read_bytes()
    return files


def _validate_bundle_graph(
    artifact_files: dict[str, bytes], *, kind: Literal["runtime", "dev-toolkit"]
) -> None:
    """Validate every shipped Markdown edge before creating the destination."""

    forbidden = ("/Users/", "/home/", "\\Users\\")
    runtime_forbidden = ("evals/", "tests/", "docs/superpowers")
    for relative, data in artifact_files.items():
        if not relative.endswith(".md"):
            continue
        document = data.decode("utf-8")
        if any(value in document for value in forbidden):
            raise ValueError(f"checkout-specific reference in {relative}")
        if kind == "runtime" and any(value in document for value in runtime_forbidden):
            raise ValueError(f"development reference in runtime document {relative}")
        parent = posixpath.dirname(relative)
        for link in _LINK_PATTERN.findall(document):
            target = link.split("#", 1)[0].split("?", 1)[0]
            if not target:
                continue
            if target.startswith("/") or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target):
                raise ValueError(f"unsupported or escaping reference {link!r} in {relative}")
            normalized = posixpath.normpath(posixpath.join(parent, target))
            if normalized == ".." or normalized.startswith("../"):
                raise ValueError(f"unsupported or escaping reference {link!r} in {relative}")
            if normalized not in artifact_files:
                raise ValueError(f"broken bundle reference {link!r} in {relative}")


def build_collection(
    source_root: Path,
    destination: Path,
    *,
    kind: Literal["runtime", "dev-toolkit"],
) -> dict[str, Any]:
    """Copy an allowlisted closed collection and return its deterministic manifest."""

    source_root = source_root.expanduser()
    destination = destination.expanduser()
    if source_root.is_symlink():
        raise ValueError(f"source root must not be a symlink: {source_root}")
    source_root = source_root.resolve()
    if kind not in {"runtime", "dev-toolkit"}:
        raise ValueError(f"unknown collection kind: {kind}")

    artifact_files = (
        _runtime_files(source_root) if kind == "runtime" else _dev_files(source_root)
    )
    _validate_bundle_graph(artifact_files, kind=kind)
    _prepare_destination(destination)
    destination = destination.resolve()
    for relative, data in sorted(artifact_files.items()):
        target = destination / relative
        if not target.resolve().is_relative_to(destination):
            raise ValueError(f"artifact path escapes destination: {relative}")
        _write(target, data)

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "collection_version": "0.1.0",
        "kind": kind,
        "files": [
            {
                "path": relative,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
            for relative, data in sorted(artifact_files.items())
        ],
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _write(destination / "manifest.json", manifest_bytes)
    return manifest


def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("source_root", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--kind", choices=("runtime", "dev-toolkit"), required=True)
    args = parser.parse_args()
    manifest = build_collection(args.source_root, args.destination, kind=args.kind)
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
