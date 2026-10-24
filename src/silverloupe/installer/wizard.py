"""Pure decision policy for the guided production installer."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

from silverloupe.contracts import InstallConfig
from silverloupe.installer.discovery import InstallDiscovery, InstallEditor, InstallHost
from silverloupe.installer.lifecycle import (
    install_release,
    uninstall_release,
    update_release,
)
from silverloupe.installer.presentation import WizardIO, human_result_rows


WizardAction: TypeAlias = Literal[
    "install", "update", "reconfigure", "uninstall", "cancel"
]


class InstallerPrerequisiteError(ValueError):
    """Raised when a safe guided installation cannot proceed."""


@dataclass(frozen=True)
class WizardSelection:
    action: WizardAction
    hosts: tuple[InstallHost, ...]
    editor: InstallEditor
    confirmed: bool


def _validate_prerequisites(discovery: InstallDiscovery) -> None:
    if not discovery.platform_supported:
        raise InstallerPrerequisiteError(
            "Silverloupe requires macOS on Apple Silicon"
        )
    if not discovery.agents:
        raise InstallerPrerequisiteError(
            "Codex or Claude Code must be installed before Silverloupe"
        )
    if discovery.free_bytes < 5 * 1024**3:
        raise InstallerPrerequisiteError(
            "Silverloupe requires at least 5 GiB of free disk space"
        )


def questions_for(discovery: InstallDiscovery) -> tuple[str, ...]:
    """Return only the decisions that materially change this installation."""

    _validate_prerequisites(discovery)
    questions: list[str] = []
    if discovery.existing_config is not None:
        questions.append("action")
    if len(discovery.agents) > 1:
        questions.append("hosts")
    if discovery.editors:
        questions.append("editor")
    questions.append("confirm")
    return tuple(questions)


def build_install_config(
    discovery: InstallDiscovery,
    selection: WizardSelection,
    version: str,
) -> InstallConfig:
    """Translate confirmed guided choices into the strict production contract."""

    if not discovery.platform_supported:
        raise InstallerPrerequisiteError(
            "Silverloupe requires macOS on Apple Silicon"
        )
    if discovery.free_bytes < 5 * 1024**3:
        raise InstallerPrerequisiteError(
            "Silverloupe requires at least 5 GiB of free disk space"
        )
    if not selection.confirmed:
        raise InstallerPrerequisiteError("installation was not confirmed")

    available_hosts = {agent.host for agent in discovery.agents}
    if not selection.hosts or not set(selection.hosts).issubset(available_hosts):
        raise InstallerPrerequisiteError("selected agent is no longer available")

    available_editors = {editor.editor for editor in discovery.editors}
    if (
        selection.editor != "report-only"
        and selection.editor not in available_editors
    ):
        raise InstallerPrerequisiteError("selected editor is no longer available")

    mode = "native" if selection.editor == "lightcraft" else "report-only"
    return InstallConfig(
        hosts=list(selection.hosts),
        scope="user",
        install_root=str(discovery.install_root),
        enabled_editors=[selection.editor],
        default_editor=selection.editor,
        integration_mode_by_editor={selection.editor: mode},
        runtime_version=version,
        model_mode="host-vision",
        model_profile={"source": "guided-installer"},
        cache_quota=5 * 1024**3,
        worker_limit=1,
        install_raw_extra=False,
    )


def _host_options(discovery: InstallDiscovery) -> tuple[tuple[str, str], ...]:
    labels = {"codex": "Codex", "claude-code": "Claude Code"}
    return tuple((agent.host, labels[agent.host]) for agent in discovery.agents)


def _editor_options(discovery: InstallDiscovery) -> tuple[tuple[str, str], ...]:
    labels = {
        "lightroom-classic": "Lightroom Classic",
        "lightroom-cloud": "Lightroom",
        "photoshop-camera-raw": "Photoshop / Camera Raw",
        "lightcraft": "LightCraft",
    }
    return tuple(
        [(editor.editor, labels[editor.editor]) for editor in discovery.editors]
        + [("report-only", "No editor — reports only")]
    )


def _doctor_rows(config: InstallConfig) -> list[tuple[str, str, str]]:
    launcher = Path(config.install_root) / "bin/silverloupe"
    try:
        completed = subprocess.run(
            [str(launcher), "doctor"],
            check=True,
            capture_output=True,
            text=True,
        )
        report = json.loads(completed.stdout)
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        return [("Doctor", "unavailable", str(error))]
    installation = report.get("installation", {}) if isinstance(report, Mapping) else {}
    return human_result_rows(
        installation if isinstance(installation, Mapping) else {}
    )


def run_installer_wizard(
    release_root: Path,
    manifest: Mapping[str, object],
    discovery: InstallDiscovery,
    io: WizardIO,
    *,
    rediscover: Callable[[], InstallDiscovery] | None = None,
) -> dict[str, object]:
    """Run the minimal guided flow and delegate mutation to the safe lifecycle."""

    questions_for(discovery)
    action: WizardAction = "install"
    if discovery.existing_config is not None:
        action_value = io.choose_one(
            "Existing installation",
            "Choose what Silverloupe should do.",
            (
                ("update", "Update"),
                ("reconfigure", "Change agents or editor"),
                ("uninstall", "Uninstall Silverloupe"),
                ("cancel", "Cancel"),
            ),
            default="update",
        )
        action = action_value  # type: ignore[assignment]
        if action == "cancel":
            return {"status": "cancelled"}
        if action == "uninstall":
            confirmed = io.confirm(
                "Uninstall Silverloupe?",
                "Only unchanged files owned by Silverloupe will be removed.",
                default=False,
            )
            if not confirmed:
                return {"status": "cancelled"}
            result = uninstall_release(discovery.existing_config)
            io.show_result(
                "Silverloupe was removed",
                [("Uninstall", str(result["status"]), "Foreign and modified files were preserved")],
            )
            return result

    if len(discovery.agents) == 1:
        hosts = (discovery.agents[0].host,)
    else:
        hosts = io.choose_many(
            "Choose agent CLIs",
            "Silverloupe installs the same six skills into every selected agent.",
            _host_options(discovery),
            defaults=tuple(agent.host for agent in discovery.agents),
        )

    if discovery.editors:
        existing_editor = (
            discovery.existing_config.default_editor
            if discovery.existing_config is not None
            else None
        )
        detected = {editor.editor for editor in discovery.editors}
        default_editor = (
            existing_editor
            if existing_editor in detected or existing_editor == "report-only"
            else discovery.editors[0].editor
        )
        editor_value = io.choose_one(
            "Choose photo editor",
            "Missing or unverified integrations safely remain report-only.",
            _editor_options(discovery),
            default=default_editor,
        )
        editor: InstallEditor = editor_value  # type: ignore[assignment]
    else:
        editor = "report-only"

    selected = WizardSelection(
        action=action,
        hosts=hosts,
        editor=editor,
        confirmed=io.confirm(
            "Install Silverloupe?",
            (
                f"Version {manifest['release_version']} will be installed in "
                f"{discovery.install_root}."
            ),
            default=True,
        ),
    )
    if not selected.confirmed:
        return {"status": "cancelled"}

    current = rediscover() if rediscover is not None else discovery
    config = build_install_config(
        current,
        selected,
        str(manifest["release_version"]),
    )
    if action == "install":
        result = install_release(release_root, config)
    else:
        result = update_release(release_root, config)

    rows = _doctor_rows(config)
    selected_agent = "Codex" if "codex" in config.hosts else "Claude Code"
    rows.extend(
        [
            ("Skills", "ready", "Six Silverloupe skills installed"),
            (
                "Next step",
                "ready",
                f"Open {selected_agent} and ask it to use Silverloupe on a photo folder",
            ),
            ("Commands", "ready", str(Path(config.install_root) / "bin/silverloupe")),
        ]
    )
    io.show_result("Silverloupe is ready", rows)
    return result
