"""Pure decision policy for the guided production installer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from silverloupe.contracts import InstallConfig
from silverloupe.installer.discovery import InstallDiscovery, InstallEditor, InstallHost


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

    _validate_prerequisites(discovery)
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
