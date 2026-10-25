"""Verified, transactional production release lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import tarfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from silverloupe.contracts import InstallConfig
from silverloupe.state import atomic_write_text


_STATE_DIRECTORY = ".silverloupe"
_VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9A-Za-z-]+)*$")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_relative(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or ".." in path.parts or "." in path.parts:
        raise ValueError(f"unsafe archive path: {name}")
    return path


def _archive_files(artifact: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    roots: set[str] = set()
    try:
        archive = tarfile.open(artifact, "r:gz")
    except (OSError, tarfile.TarError) as error:
        raise ValueError(f"invalid release archive: {error}") from error
    with archive:
        seen: set[str] = set()
        for member in archive.getmembers():
            path = _safe_relative(member.name)
            if member.name in seen:
                raise ValueError(f"duplicate archive member: {member.name}")
            seen.add(member.name)
            roots.add(path.parts[0])
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError(f"release archive contains a link or special file: {member.name}")
            if len(path.parts) < 2:
                raise ValueError(f"release archive member lacks a root directory: {member.name}")
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError(f"release archive member is unreadable: {member.name}")
            relative = PurePosixPath(*path.parts[1:]).as_posix()
            if relative in files:
                raise ValueError(f"duplicate release path: {relative}")
            files[relative] = handle.read()
    if len(roots) != 1:
        raise ValueError("release archive must contain exactly one root directory")
    return files


def _directory_files(artifact: Path) -> dict[str, bytes]:
    if artifact.is_symlink() or not artifact.is_dir():
        raise ValueError(f"release directory is missing or unsafe: {artifact}")
    files: dict[str, bytes] = {}
    for candidate in sorted(artifact.rglob("*")):
        if candidate.is_symlink():
            raise ValueError(f"release directory contains a symlink: {candidate}")
        if candidate.is_file():
            relative = candidate.relative_to(artifact).as_posix()
            _safe_relative(relative)
            files[relative] = candidate.read_bytes()
    return files


def _verified_release(artifact: Path) -> tuple[dict[str, bytes], dict[str, Any]]:
    artifact = artifact.expanduser().resolve()
    if not artifact.exists():
        raise FileNotFoundError(f"release artifact does not exist: {artifact}")
    files = _directory_files(artifact) if artifact.is_dir() else _archive_files(artifact)
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
    if not isinstance(ownership, list) or any(not isinstance(value, str) for value in ownership):
        raise ValueError("release ownership list is invalid")
    if len(ownership) != len(set(ownership)) or set(ownership) != set(files):
        raise ValueError("release ownership does not exactly match artifact files")
    payload = set(files) - {"manifest.json", "checksums.json"}
    if set(checksums) != payload:
        raise ValueError("release checksums do not exactly cover payload files")
    for relative, expected in checksums.items():
        if not isinstance(expected, str) or _sha256(files[relative]) != expected:
            raise ValueError(f"checksum mismatch for {relative}")
    version = manifest.get("release_version")
    if not isinstance(version, str) or not _VERSION_PATTERN.fullmatch(version):
        raise ValueError("release manifest contains an invalid version")
    return files, manifest


def _host_skill_root(config: InstallConfig, host: str) -> Path:
    base = (
        Path.home()
        if config.scope == "user"
        else Path(config.install_root).expanduser().resolve()
    )
    if host == "codex":
        return base / ".agents/skills"
    if host == "claude-code":
        return base / ".claude/skills"
    raise ValueError(f"unknown host: {host}")


def _state_root(install_root: Path) -> Path:
    return install_root / _STATE_DIRECTORY


def load_installed_config(install_root: Path) -> InstallConfig:
    """Load the active strict installer configuration."""

    path = _state_root(install_root.expanduser().resolve()) / "config.json"
    try:
        return InstallConfig.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise FileNotFoundError(f"Silverloupe is not installed at {install_root}") from error


def _load_ownership(install_root: Path) -> dict[str, Any] | None:
    path = _state_root(install_root) / "ownership.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"installed ownership metadata is invalid: {error}") from error
    if not isinstance(value, dict) or not isinstance(value.get("files"), list):
        raise ValueError("installed ownership metadata is invalid")
    return value


def _ownership_map(ownership: dict[str, Any] | None) -> dict[Path, str]:
    if ownership is None:
        return {}
    result: dict[Path, str] = {}
    for entry in ownership.get("files", []):
        if isinstance(entry, dict) and isinstance(entry.get("path"), str) and isinstance(
            entry.get("sha256"), str
        ):
            result[Path(entry["path"])] = entry["sha256"]
    return result


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _remove_path(path: Path) -> None:
    details = _lstat(path)
    if details is None:
        return
    if stat.S_ISDIR(details.st_mode) and not stat.S_ISLNK(details.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _path_has_symlink_component(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    components = [current]
    for part in relative.parts[:-1]:
        current = current / part
        components.append(current)
    return any(
        details is not None and stat.S_ISLNK(details.st_mode)
        for details in (_lstat(component) for component in components)
    )


def _owned_root(path: Path, roots: list[Path]) -> Path | None:
    for root in roots:
        if path == root or path.is_relative_to(root):
            return root
    return None


def _snapshot_targets(targets: list[Path], snapshot_root: Path) -> list[tuple[Path, Path | None]]:
    snapshots: list[tuple[Path, Path | None]] = []
    for index, target in enumerate(targets):
        details = _lstat(target)
        if details is None:
            snapshots.append((target, None))
            continue
        if stat.S_ISLNK(details.st_mode):
            raise ValueError(f"transaction target is a symlink: {target}")
        snapshot = snapshot_root / str(index)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        if stat.S_ISDIR(details.st_mode):
            shutil.copytree(target, snapshot, symlinks=True)
        elif stat.S_ISREG(details.st_mode):
            shutil.copy2(target, snapshot)
        else:
            raise ValueError(f"transaction target is not a regular file or directory: {target}")
        snapshots.append((target, snapshot))
    return snapshots


def _restore_targets(snapshots: list[tuple[Path, Path | None]]) -> None:
    for target, snapshot in reversed(snapshots):
        _remove_path(target)
        if snapshot is None:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if snapshot.is_dir():
            shutil.copytree(snapshot, target, symlinks=True)
        else:
            shutil.copy2(snapshot, target)


def _write_stage(stage: Path, files: dict[str, bytes]) -> Path:
    release = stage / "release"
    for relative, data in sorted(files.items()):
        destination = release.joinpath(*PurePosixPath(relative).parts)
        if not destination.resolve().is_relative_to(release.resolve()):
            raise ValueError(f"release path escapes staging root: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        destination.chmod(0o755 if relative == "install.py" else 0o644)
    return release


def _skill_files(directory: Path) -> list[Path]:
    return [
        path
        for path in directory.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.relative_to(directory).parts
        and path.suffix not in {".pyc", ".pyo"}
    ]


def _preflight_host_targets(
    release: Path,
    config: InstallConfig,
    prior_owned: dict[Path, str],
) -> None:
    skills = release / "skills"
    for host in config.hosts:
        root = _host_skill_root(config, host)
        for source in sorted(path for path in skills.iterdir() if path.is_dir()):
            target = root / source.name
            if not target.exists():
                continue
            if target.is_symlink() or not target.is_dir():
                raise ValueError(f"host skill target is not an owned directory: {target}")
            for existing in _skill_files(target):
                expected = prior_owned.get(existing)
                if expected is None or _sha256(existing.read_bytes()) != expected:
                    raise ValueError(f"host skill contains foreign or modified file: {existing}")


def _rewrite_skill_commands(skill: Path, launcher: Path) -> None:
    document = skill.read_text(encoding="utf-8")
    command = shlex.quote(str(launcher))
    document = re.sub(r"(?m)^silverloupe(?=\s)", command, document)
    document = document.replace("`silverloupe ", f"`{command} ")
    skill.write_text(document, encoding="utf-8")


def _install_host_skills(
    release: Path,
    config: InstallConfig,
    launcher: Path,
) -> tuple[list[Path], list[Path]]:
    files: list[Path] = []
    directories: list[Path] = []
    for host in config.hosts:
        root = _host_skill_root(config, host)
        root.mkdir(parents=True, exist_ok=True)
        for source in sorted(path for path in (release / "skills").iterdir() if path.is_dir()):
            target = root / source.name
            temporary = root / f".{source.name}.silverloupe-{uuid.uuid4().hex}"
            shutil.copytree(source, temporary)
            _rewrite_skill_commands(temporary / "SKILL.md", launcher)
            if target.exists():
                shutil.rmtree(target)
            temporary.replace(target)
            directories.extend(
                [path for path in target.rglob("*") if path.is_dir()] + [target]
            )
            files.extend(_skill_files(target))
    return files, directories


def _write_launcher(install_root: Path, version_root: Path, config: InstallConfig) -> Path:
    launcher = install_root / "bin/silverloupe"
    requirements_name = (
        "requirements-raw.lock" if config.install_raw_extra else "requirements.lock"
    )
    requirements = version_root / "runtime" / requirements_name
    runtime_source = version_root / "runtime/src"
    if not requirements.is_file():
        raise ValueError(f"installed runtime lock is missing: {requirements}")
    configured_uv = config.model_profile.get("uv_executable", "uv")
    uv_executable = (
        configured_uv
        if isinstance(configured_uv, str) and configured_uv.strip()
        else "uv"
    )
    script = "\n".join(
        [
            "#!/bin/sh",
            "set -eu",
            "export PYTHONDONTWRITEBYTECODE=1",
            f"export PYTHONPATH={shlex.quote(str(runtime_source))}",
            f"export SILVERLOUPE_INSTALL_ROOT={shlex.quote(str(install_root))}",
            f"exec {shlex.quote(uv_executable)} run --python 3.12 --no-project "
            f"--with-requirements {shlex.quote(str(requirements))} "
            "python -c 'from silverloupe.cli import main; raise SystemExit(main())' "
            '"$@"',
            "",
        ]
    )
    atomic_write_text(launcher, script)
    launcher.chmod(0o755)
    return launcher


def _file_entries(paths: list[Path]) -> list[dict[str, str]]:
    return [
        {"path": str(path.resolve()), "sha256": _sha256(path.read_bytes())}
        for path in sorted(set(paths), key=lambda value: str(value))
    ]


def _remove_owned_files(
    entries: list[dict[str, Any]],
    keep: set[Path],
    roots: list[Path],
) -> list[str]:
    preserved: list[str] = []
    for entry in sorted(entries, key=lambda value: len(str(value.get("path", ""))), reverse=True):
        raw_path = entry.get("path")
        expected = entry.get("sha256")
        if not isinstance(raw_path, str) or not isinstance(expected, str):
            continue
        path = Path(raw_path)
        if path in keep:
            continue
        root = _owned_root(path, roots)
        details = _lstat(path)
        if details is None:
            continue
        if (
            root is None
            or _path_has_symlink_component(path, root)
            or not stat.S_ISREG(details.st_mode)
            or _sha256(path.read_bytes()) != expected
        ):
            preserved.append(str(path))
            continue
        path.unlink()
    return preserved


def _remove_empty_directories(values: list[str], roots: list[Path]) -> None:
    for raw in sorted(set(values), key=len, reverse=True):
        path = Path(raw)
        root = _owned_root(path, roots)
        details = _lstat(path)
        if (
            root is None
            or details is None
            or _path_has_symlink_component(path, root)
            or not stat.S_ISDIR(details.st_mode)
        ):
            continue
        try:
            path.rmdir()
        except OSError:
            pass


def _activate_release(
    artifact: Path,
    config: InstallConfig,
    *,
    failpoint: str | None,
) -> dict[str, Any]:
    files, manifest = _verified_release(artifact)
    version = manifest["release_version"]
    if version != config.runtime_version:
        raise ValueError(
            f"artifact version {version!r} does not match configured runtime {config.runtime_version!r}"
        )
    install_root = Path(config.install_root).expanduser().resolve()
    state_root = _state_root(install_root)
    if _lstat(state_root) is not None and _path_has_symlink_component(
        state_root / "ownership.json", install_root
    ):
        raise ValueError("installer state path contains a symlink")
    prior = _load_ownership(install_root) if install_root.exists() else None
    prior_owned = _ownership_map(prior)
    active_path = state_root / "active-version"
    if active_path.is_file() and active_path.read_text(encoding="utf-8") == f"{version}\n":
        try:
            if load_installed_config(install_root) == config:
                return {"status": "unchanged", "active_version": version}
        except (FileNotFoundError, ValueError):
            pass

    stage = install_root.parent / f".{install_root.name}.staging-{uuid.uuid4().hex}"
    if stage.exists():
        raise ValueError(f"staging path already exists: {stage}")
    try:
        release = _write_stage(stage, files)
        _preflight_host_targets(release, config, prior_owned)
        if failpoint == "after_stage":
            raise RuntimeError("injected update failure after_stage")

        install_root.mkdir(parents=True, exist_ok=True)
        versions = install_root / "versions"
        versions.mkdir(exist_ok=True)
        version_root = versions / version
        if not version_root.resolve().is_relative_to(versions.resolve()):
            raise ValueError("release version escapes versions directory")
        launcher = install_root / "bin/silverloupe"
        launcher_details = _lstat(launcher)
        if launcher_details is not None:
            expected_launcher = prior_owned.get(launcher)
            if (
                expected_launcher is None
                or not stat.S_ISREG(launcher_details.st_mode)
                or _sha256(launcher.read_bytes()) != expected_launcher
            ):
                raise ValueError(f"launcher target is not owned by Silverloupe: {launcher}")
        if prior is None and _lstat(state_root) is not None:
            raise ValueError(f"installer state is unowned: {state_root}")

        skill_targets = [
            _host_skill_root(config, host) / skill.name
            for host in config.hosts
            for skill in sorted(
                (path for path in release.joinpath("skills").iterdir() if path.is_dir()),
                key=lambda value: value.name,
            )
        ]
        live_targets = [
            launcher,
            state_root / "config.json",
            active_path,
            state_root / "ownership.json",
            *skill_targets,
        ]
        snapshots = _snapshot_targets(live_targets, stage / "rollback")
        created_version = False
        try:
            if not version_root.exists():
                (stage / "release").replace(version_root)
                created_version = True
            else:
                staged_hashes = {
                    path.relative_to(release).as_posix(): _sha256(path.read_bytes())
                    for path in _skill_files(release)
                }
                installed_hashes = {
                    path.relative_to(version_root).as_posix(): _sha256(path.read_bytes())
                    for path in _skill_files(version_root)
                }
                if staged_hashes != installed_hashes:
                    raise ValueError(f"installed version directory is not identical: {version_root}")

            launcher = _write_launcher(install_root, version_root, config)
            host_files, host_directories = _install_host_skills(
                version_root, config, launcher
            )
            version_files = _skill_files(version_root)
            version_directories = [
                path for path in version_root.rglob("*") if path.is_dir()
            ] + [version_root, versions]
            state_root.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                state_root / "config.json", config.model_dump_json(indent=2) + "\n"
            )
            atomic_write_text(active_path, f"{version}\n")
            owned_files = version_files + host_files + [launcher]
            ownership_roots = [
                install_root,
                *[_host_skill_root(config, host) for host in config.hosts],
            ]
            ownership = {
                "schema_version": 1,
                "active_version": version,
                "files": _file_entries(owned_files),
                "directories": [
                    str(path.resolve())
                    for path in sorted(
                        set(version_directories + host_directories + [launcher.parent]),
                        key=lambda value: str(value),
                    )
                ],
                "roots": [str(path) for path in ownership_roots],
                "state_files": [
                    str((state_root / "active-version").resolve()),
                    str((state_root / "config.json").resolve()),
                    str((state_root / "ownership.json").resolve()),
                ],
            }
            atomic_write_text(
                state_root / "ownership.json",
                json.dumps(ownership, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
        except Exception:
            _restore_targets(snapshots)
            if created_version:
                _remove_path(version_root)
            raise

        keep = {Path(entry["path"]) for entry in ownership["files"]}
        if prior is not None:
            prior_roots = [Path(value) for value in prior.get("roots", [])]
            if not prior_roots:
                prior_roots = [install_root, *ownership_roots[1:]]
            _remove_owned_files(prior.get("files", []), keep, prior_roots)
            _remove_empty_directories(prior.get("directories", []), prior_roots)
        status = "updated" if prior is not None else "installed"
        return {
            "status": status,
            "active_version": version,
            "hosts": list(config.hosts),
            "default_editor": config.default_editor,
        }
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def install_release(artifact: Path, config: InstallConfig) -> dict[str, Any]:
    """Install or idempotently reapply one verified production artifact."""

    return _activate_release(artifact, config, failpoint=None)


def update_release(
    artifact: Path,
    config: InstallConfig,
    *,
    failpoint: str | None = None,
) -> dict[str, Any]:
    """Prepare and activate an update after the complete release verifies."""

    if failpoint not in {None, "after_stage"}:
        raise ValueError(f"unknown update failpoint: {failpoint}")
    return _activate_release(artifact, config, failpoint=failpoint)


def uninstall_release(config: InstallConfig) -> dict[str, Any]:
    """Remove only unchanged files recorded as owned by this installation."""

    install_root = Path(config.install_root).expanduser().resolve()
    state_root = _state_root(install_root)
    if _path_has_symlink_component(state_root / "ownership.json", install_root):
        raise ValueError("installer state path contains a symlink")
    ownership = _load_ownership(install_root)
    if ownership is None:
        return {"status": "not-installed", "preserved": []}
    roots = [Path(value) for value in ownership.get("roots", []) if isinstance(value, str)]
    if not roots:
        roots = [
            install_root,
            *[_host_skill_root(config, host) for host in config.hosts],
        ]
    preserved = _remove_owned_files(ownership.get("files", []), set(), roots)
    for raw in ownership.get("state_files", []):
        if not isinstance(raw, str):
            continue
        path = Path(raw)
        if path == state_root / "ownership.json":
            continue
        root = _owned_root(path, roots)
        details = _lstat(path)
        if (
            root is not None
            and details is not None
            and not _path_has_symlink_component(path, root)
            and stat.S_ISREG(details.st_mode)
        ):
            path.unlink()
    ownership_path = state_root / "ownership.json"
    ownership_details = _lstat(ownership_path)
    if ownership_details is not None and stat.S_ISREG(ownership_details.st_mode):
        ownership_path.unlink()
    _remove_empty_directories(ownership.get("directories", []), roots)
    _remove_empty_directories(
        [str(state_root), str(install_root / "versions")], roots
    )
    return {"status": "uninstalled", "preserved": sorted(preserved)}


def read_release_manifest(artifact: Path) -> dict[str, Any]:
    """Return verified release metadata without installing it."""

    _files, manifest = _verified_release(artifact)
    return manifest
