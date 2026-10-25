"""Read-only discovery for the production installer."""

from __future__ import annotations

import platform
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from silverloupe.contracts import InstallConfig
from silverloupe.installer.lifecycle import load_installed_config


InstallEditor = Literal[
    "lightroom-classic",
    "lightroom-cloud",
    "photoshop-camera-raw",
    "lightcraft",
    "report-only",
]
InstallHost = Literal["codex", "claude-code"]

_EDITOR_ORDER: tuple[InstallEditor, ...] = (
    "lightroom-classic",
    "lightroom-cloud",
    "photoshop-camera-raw",
    "lightcraft",
)


@dataclass(frozen=True)
class AgentInstallDiscovery:
    host: InstallHost
    executable: Path


@dataclass(frozen=True)
class EditorInstallDiscovery:
    editor: InstallEditor
    application_path: Path


@dataclass(frozen=True)
class InstallDiscovery:
    system: str
    machine: str
    agents: tuple[AgentInstallDiscovery, ...]
    editors: tuple[EditorInstallDiscovery, ...]
    uv_path: Path | None
    python_312_available: bool
    free_bytes: int
    install_root: Path
    existing_config: InstallConfig | None
    diagnostics: tuple[str, ...] = ()

    @property
    def platform_supported(self) -> bool:
        return self.system == "Darwin" and self.machine in {"arm64", "aarch64"}


def default_install_root(home: Path) -> Path:
    """Return the fixed per-user installation root on macOS."""

    return home.expanduser() / "Library/Application Support/Silverloupe"


def _editor_for_application(path: Path) -> InstallEditor | None:
    name = path.name.casefold()
    if "lightroom classic" in name:
        return "lightroom-classic"
    if name in {"adobe lightroom.app", "lightroom.app"}:
        return "lightroom-cloud"
    if "photoshop" in name:
        return "photoshop-camera-raw"
    if name == "lightcraft.app":
        return "lightcraft"
    return None


def _spotlight_applications() -> tuple[Path, ...]:
    try:
        completed = subprocess.run(
            ["mdfind", "kMDItemContentType == 'com.apple.application-bundle'"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return ()
    return tuple(Path(line) for line in completed.stdout.splitlines() if line.strip())


def _existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _default_python_probe(uv_path: Path | None) -> bool:
    if sys.version_info[:2] == (3, 12):
        return True
    if uv_path is None:
        return False
    try:
        return subprocess.run(
            [str(uv_path), "python", "find", "3.12"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0
    except OSError:
        return False


def discover_install_environment(
    install_root: Path,
    *,
    system: str | None = None,
    machine: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    application_roots: Iterable[Path] | None = None,
    spotlight: Callable[[], Iterable[Path]] = _spotlight_applications,
    disk_usage: Callable[[Path], object] = shutil.disk_usage,
    python_312_probe: Callable[[], bool] | None = None,
) -> InstallDiscovery:
    """Discover supported hosts, applications, and prerequisites without mutation."""

    root = install_root.expanduser().resolve()
    observed_system = system or platform.system()
    observed_machine = machine or platform.machine()

    agents: list[AgentInstallDiscovery] = []
    for command, host in (("codex", "codex"), ("claude", "claude-code")):
        executable = which(command)
        if executable:
            agents.append(AgentInstallDiscovery(host, Path(executable).resolve()))

    uv_value = which("uv")
    uv_path = Path(uv_value).resolve() if uv_value else None

    if application_roots is None:
        configured_roots = os.environ.get("SILVERLOUPE_APPLICATION_ROOTS")
        if configured_roots is None:
            application_roots = (Path("/Applications"), Path.home() / "Applications")
        else:
            application_roots = tuple(
                Path(value)
                for value in configured_roots.split(os.pathsep)
                if value
            )
    candidates: set[Path] = set()
    for application_root in application_roots:
        if application_root.is_dir():
            candidates.update(application_root.glob("*.app"))
    if os.environ.get("SILVERLOUPE_DISABLE_SPOTLIGHT") != "1":
        candidates.update(Path(value) for value in spotlight())

    by_editor: dict[InstallEditor, Path] = {}
    for candidate in sorted(candidates, key=lambda value: str(value)):
        editor = _editor_for_application(candidate)
        if editor is not None and editor not in by_editor:
            by_editor[editor] = candidate.expanduser().resolve()
    editors = tuple(
        EditorInstallDiscovery(editor, by_editor[editor])
        for editor in _EDITOR_ORDER
        if editor in by_editor
    )

    diagnostics: list[str] = []
    try:
        existing = load_installed_config(root)
    except FileNotFoundError:
        existing = None
    except ValueError:
        existing = None
        diagnostics.append("existing Silverloupe configuration is invalid")

    usage = disk_usage(_existing_parent(root))
    free_bytes = getattr(usage, "free")
    if not isinstance(free_bytes, int):
        raise TypeError("disk_usage must return an object with integer free bytes")
    python_ready = (
        python_312_probe() if python_312_probe is not None else _default_python_probe(uv_path)
    )

    return InstallDiscovery(
        system=observed_system,
        machine=observed_machine,
        agents=tuple(agents),
        editors=editors,
        uv_path=uv_path,
        python_312_available=python_ready,
        free_bytes=free_bytes,
        install_root=root,
        existing_config=existing,
        diagnostics=tuple(diagnostics),
    )
