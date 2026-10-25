from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from tools.build_collection import (
    PRODUCTION_SKILLS,
    ReleaseAssets,
    build_release,
    build_release_assets,
)


ROOT = Path(__file__).parents[2]
LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def _extract(archive: Path, destination: Path) -> Path:
    with tarfile.open(archive, "r:gz") as handle:
        handle.extractall(destination, filter="data")
    return destination / "silverloupe-0.1.0"


def test_release_contains_closed_runtime_without_development_materials(tmp_path: Path) -> None:
    archive = build_release(ROOT, tmp_path / "dist", "0.1.0")
    release = _extract(archive, tmp_path / "unpacked")

    assert {path.name for path in (release / "skills").iterdir()} == set(PRODUCTION_SKILLS)
    assert {
        "USER_GUIDE.md",
        "adapters",
        "checksums.json",
        "install.py",
        "manifest.json",
        "runtime",
        "skills",
    }.issubset({path.name for path in release.iterdir()})

    forbidden_parts = {
        ".pytest_cache",
        "__pycache__",
        "dev",
        "evals",
        "gold",
        "tests",
    }
    for path in release.rglob("*"):
        assert forbidden_parts.isdisjoint(path.relative_to(release).parts)
        assert not path.is_symlink()

    pyproject = (release / "runtime/pyproject.toml").read_text(encoding="utf-8")
    assert "pytest" not in pyproject
    assert "jsonschema" not in pyproject
    assert "dependency-groups" not in pyproject
    assert not (release / "runtime/uv.lock").exists()
    production_lock = (release / "runtime/requirements.lock").read_text(encoding="utf-8")
    raw_lock = (release / "runtime/requirements-raw.lock").read_text(encoding="utf-8")
    assert "pytest" not in production_lock
    assert "jsonschema" not in production_lock
    assert "rawpy" not in production_lock
    assert "rawpy" in raw_lock


def test_release_markdown_references_are_artifact_local(tmp_path: Path) -> None:
    release = _extract(
        build_release(ROOT, tmp_path / "dist", "0.1.0"),
        tmp_path / "unpacked",
    )

    for document in (release / "skills").rglob("*.md"):
        for link in LINK_PATTERN.findall(document.read_text(encoding="utf-8")):
            target = link.split("#", 1)[0].split("?", 1)[0]
            if not target:
                continue
            assert ":" not in target
            resolved = (document.parent / target).resolve()
            assert resolved.is_relative_to(release.resolve())
            assert resolved.is_file(), f"broken link {link!r} in {document}"


def test_release_checksums_cover_payload_and_match_bytes(tmp_path: Path) -> None:
    release = _extract(
        build_release(ROOT, tmp_path / "dist", "0.1.0"),
        tmp_path / "unpacked",
    )
    checksums = json.loads((release / "checksums.json").read_text(encoding="utf-8"))
    expected_payload = {
        path.relative_to(release).as_posix()
        for path in release.rglob("*")
        if path.is_file() and path.name not in {"checksums.json", "manifest.json"}
    }

    assert set(checksums) == expected_payload
    for relative, expected in checksums.items():
        assert hashlib.sha256((release / relative).read_bytes()).hexdigest() == expected

    manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["release_version"] == "0.1.0"
    assert manifest["skills"] == list(PRODUCTION_SKILLS)
    assert manifest["runtime_dependencies"] == [
        "numpy>=2.2,<3",
        "pillow>=11,<13",
        "pydantic>=2.12,<3",
    ]
    assert "--with-requirements runtime/requirements.lock" in manifest["bootstrap"]
    assert set(manifest["ownership"]) == expected_payload | {"checksums.json", "manifest.json"}


def test_release_runtime_imports_without_checkout_or_evals(tmp_path: Path) -> None:
    release = _extract(
        build_release(ROOT, tmp_path / "dist", "0.1.0"),
        tmp_path / "unpacked",
    )
    runtime_source = release / "runtime/src"
    script = (
        "import importlib.util, pathlib, sys; "
        f"sys.path[:] = [{str(runtime_source)!r}]; "
        "import silverloupe; "
        "assert silverloupe.__version__ == '0.1.0'; "
        "assert importlib.util.find_spec('evals') is None; "
        "print(pathlib.Path(silverloupe.__file__).resolve())"
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        check=True,
        capture_output=True,
        text=True,
    )

    assert str(runtime_source) in completed.stdout
    assert str(ROOT) not in completed.stdout


def test_release_archive_is_reproducible_and_existing_output_is_preserved(
    tmp_path: Path,
) -> None:
    first = build_release(ROOT, tmp_path / "one", "0.1.0")
    second = build_release(ROOT, tmp_path / "two", "0.1.0")

    assert first.read_bytes() == second.read_bytes()
    original = first.read_bytes()
    with pytest.raises(FileExistsError):
        build_release(ROOT, tmp_path / "one", "0.1.0")
    assert first.read_bytes() == original


@pytest.mark.parametrize("version", ["", "../escape", "1/2", "release candidate"])
def test_release_rejects_versions_that_can_escape_or_ambiguate_archive_paths(
    tmp_path: Path,
    version: str,
) -> None:
    with pytest.raises(ValueError, match="version"):
        build_release(ROOT, tmp_path / "dist", version)


def test_bootstrap_entry_point_runs_without_importing_the_runtime(tmp_path: Path) -> None:
    release = _extract(
        build_release(ROOT, tmp_path / "dist", "0.1.0"),
        tmp_path / "unpacked",
    )

    completed = subprocess.run(
        [sys.executable, str(release / "install.py"), "--help"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--host" in completed.stdout
    assert "--editor" in completed.stdout


def test_release_assets_include_archive_checksum_and_executable_bootstrap(
    tmp_path: Path,
) -> None:
    assets = build_release_assets(ROOT, tmp_path / "dist", "0.1.0")

    assert isinstance(assets, ReleaseAssets)
    assert assets.archive.name == "silverloupe-0.1.0.tar.gz"
    assert assets.checksum.name == "silverloupe-0.1.0.tar.gz.sha256"
    assert assets.bootstrap.name == "install.sh"
    assert stat.S_IMODE(assets.bootstrap.stat().st_mode) == 0o755
    assert assets.bootstrap.read_bytes() == (ROOT / "install.sh").read_bytes()


def test_release_checksum_uses_exact_archive_basename_and_bytes(tmp_path: Path) -> None:
    assets = build_release_assets(ROOT, tmp_path / "dist", "0.1.0")

    assert assets.checksum.read_text(encoding="utf-8") == (
        f"{hashlib.sha256(assets.archive.read_bytes()).hexdigest()}  "
        f"{assets.archive.name}\n"
    )


def test_release_assets_are_reproducible_and_preserve_existing_outputs(
    tmp_path: Path,
) -> None:
    first = build_release_assets(ROOT, tmp_path / "one", "0.1.0")
    second = build_release_assets(ROOT, tmp_path / "two", "0.1.0")

    assert first.archive.read_bytes() == second.archive.read_bytes()
    assert first.checksum.read_bytes() == second.checksum.read_bytes()
    assert first.bootstrap.read_bytes() == second.bootstrap.read_bytes()
    before = {
        path: path.read_bytes()
        for path in (first.archive, first.checksum, first.bootstrap)
    }
    with pytest.raises(FileExistsError):
        build_release_assets(ROOT, tmp_path / "one", "0.1.0")
    assert {path: path.read_bytes() for path in before} == before

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "install.sh").write_text("foreign", encoding="utf-8")
    with pytest.raises(FileExistsError):
        build_release_assets(ROOT, occupied, "0.1.0")
    assert not (occupied / "silverloupe-0.1.0.tar.gz").exists()
    assert (occupied / "install.sh").read_text(encoding="utf-8") == "foreign"


def test_bootstrap_source_is_not_embedded_as_an_unverified_runtime_replacement(
    tmp_path: Path,
) -> None:
    assets = build_release_assets(ROOT, tmp_path / "dist", "0.1.0")
    release = _extract(assets.archive, tmp_path / "unpacked")
    manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))

    assert not (release / "install.sh").exists()
    assert "install.sh" not in manifest["ownership"]
    assert (release / "install.py").is_file()
