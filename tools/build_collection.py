"""Build closed, deterministic Silverloupe skill collections."""

from __future__ import annotations

import hashlib
import gzip
import io
import json
import posixpath
import re
import shutil
import tarfile
import tempfile
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
    "../../resources/adapters/lightroom.md": "references/adapters/lightroom.md",
    "../../resources/adapters/photoshop.md": "references/adapters/photoshop.md",
    "../../resources/adapters/lightcraft.md": "references/adapters/lightcraft.md",
}
_DEV_REFERENCES = {
    "../../../evals/cases/routing.jsonl": "references/routing.jsonl",
    "../../../evals/baselines/issue-6-b2.jsonl": "references/issue-6-b2.jsonl",
}

_VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9A-Za-z-]+)*$")


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


def _production_pyproject(source: Path) -> bytes:
    """Return the project metadata without development dependency groups."""

    text = source.read_text(encoding="utf-8")
    text = re.sub(
        r"\n\[dependency-groups\]\n.*?(?=\n\[[^\n]+\]\n|\Z)",
        "\n",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"\n\[tool\.pytest\.ini_options\]\n.*?(?=\n\[[^\n]+\]\n|\Z)",
        "\n",
        text,
        flags=re.DOTALL,
    )
    return text.encode("utf-8")


def _tree_files(source_root: Path, relative_root: str, target_root: str) -> dict[str, bytes]:
    source = source_root / relative_root
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f"release source tree is missing or unsafe: {source}")
    files: dict[str, bytes] = {}
    for candidate in sorted(source.rglob("*")):
        if candidate.is_symlink():
            raise ValueError(f"release source tree contains a symlink: {candidate}")
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(source)
        if "__pycache__" in relative.parts or candidate.suffix in {".pyc", ".pyo"}:
            continue
        target = posixpath.join(target_root, relative.as_posix())
        files[target] = candidate.read_bytes()
    return files


def _release_payload(source_root: Path) -> tuple[dict[str, bytes], dict[str, Any]]:
    template_path = _safe_source(
        source_root / "packaging/production-manifest.json", source_root
    )
    template = json.loads(template_path.read_text(encoding="utf-8"))
    if template.get("skills") != list(PRODUCTION_SKILLS):
        raise ValueError("production manifest must declare exactly the six production skills")

    files = _runtime_files(source_root)
    payload: dict[str, bytes] = dict(files)
    payload.update(_tree_files(source_root, "src/silverloupe", "runtime/src/silverloupe"))
    payload.update(_tree_files(source_root, "resources/adapters", "adapters"))
    payload["install.py"] = _safe_source(
        source_root / "packaging/install.py", source_root
    ).read_bytes()
    payload["USER_GUIDE.md"] = _safe_source(
        source_root / "docs/user-installation.md", source_root
    ).read_bytes()
    payload["runtime/uv.lock"] = _safe_source(source_root / "uv.lock", source_root).read_bytes()
    payload["runtime/pyproject.toml"] = _production_pyproject(
        _safe_source(source_root / "pyproject.toml", source_root)
    )
    return payload, template


def _release_tar_bytes(root_name: str, files: dict[str, bytes]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for relative, data in sorted(files.items()):
            info = tarfile.TarInfo(f"{root_name}/{relative}")
            info.size = len(data)
            info.mode = 0o755 if relative == "install.py" else 0o644
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(data))
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", filename="", mtime=0) as handle:
        handle.write(raw.getvalue())
    return compressed.getvalue()


def build_release(source: Path, output: Path, version: str) -> Path:
    """Build one deterministic, allowlisted Silverloupe production archive."""

    if not _VERSION_PATTERN.fullmatch(version):
        raise ValueError("version must be a non-empty dot-separated release identifier")
    if source.is_symlink():
        raise ValueError("release source must not be a symlink")
    source = source.expanduser().resolve()
    output = output.expanduser()
    if output.is_symlink():
        raise ValueError("release output must not be a symlink")
    output.mkdir(parents=True, exist_ok=True)
    output = output.resolve()
    destination = output / f"silverloupe-{version}.tar.gz"
    if destination.exists():
        raise FileExistsError(f"release artifact already exists: {destination}")

    payload, template = _release_payload(source)
    _validate_bundle_graph(payload, kind="runtime")
    checksums = {
        relative: hashlib.sha256(data).hexdigest()
        for relative, data in sorted(payload.items())
    }
    checksums_bytes = (
        json.dumps(checksums, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    ownership = sorted([*payload, "checksums.json", "manifest.json"])
    manifest = {
        **template,
        "release_version": version,
        "core_version": version,
        "skills_version": version,
        "schema_version": template["schema_version"],
        "template_version": 1,
        "ownership": ownership,
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    artifact_files = {
        **payload,
        "checksums.json": checksums_bytes,
        "manifest.json": manifest_bytes,
    }
    archive_bytes = _release_tar_bytes(f"silverloupe-{version}", artifact_files)

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=output,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(archive_bytes)
            handle.flush()
        temporary.replace(destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return destination


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
