from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from silverloupe.contracts import InstallConfig
from silverloupe.installer.discovery import (
    AgentInstallDiscovery,
    EditorInstallDiscovery,
    InstallDiscovery,
    default_install_root,
    discover_install_environment,
)
from silverloupe.installer.wizard import (
    InstallerPrerequisiteError,
    WizardSelection,
    build_install_config,
    questions_for,
)


def _disk_usage(_path: Path) -> SimpleNamespace:
    return SimpleNamespace(free=20 * 1024**3)


@pytest.mark.parametrize(
    ("system", "machine"),
    [("Linux", "aarch64"), ("Darwin", "x86_64")],
)
def test_discovery_rejects_non_macos_and_non_arm64(
    tmp_path: Path,
    system: str,
    machine: str,
) -> None:
    discovery = discover_install_environment(
        tmp_path / "install",
        system=system,
        machine=machine,
        which=lambda _name: None,
        application_roots=(),
        spotlight=lambda: (),
        disk_usage=_disk_usage,
        python_312_probe=lambda: False,
    )

    assert discovery.platform_supported is False
    assert discovery.system == system
    assert discovery.machine == machine


def test_discovery_records_resolved_codex_and_claude_paths(tmp_path: Path) -> None:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    codex = binaries / "codex"
    claude = binaries / "claude"
    codex.touch()
    claude.touch()

    discovery = discover_install_environment(
        tmp_path / "install",
        system="Darwin",
        machine="arm64",
        which=lambda name: str({"codex": codex, "claude": claude, "uv": codex}[name]),
        application_roots=(),
        spotlight=lambda: (),
        disk_usage=_disk_usage,
        python_312_probe=lambda: True,
    )

    assert [(agent.host, agent.executable) for agent in discovery.agents] == [
        ("codex", codex.resolve()),
        ("claude-code", claude.resolve()),
    ]
    assert discovery.uv_path == codex.resolve()
    assert discovery.python_312_available is True


def test_discovery_finds_supported_apps_without_claiming_integration(tmp_path: Path) -> None:
    applications = tmp_path / "Applications"
    lightcraft = applications / "LightCraft.app"
    photoshop = applications / "Adobe Photoshop 2026.app"
    lightcraft.mkdir(parents=True)
    photoshop.mkdir()

    discovery = discover_install_environment(
        tmp_path / "install",
        system="Darwin",
        machine="arm64",
        which=lambda _name: None,
        application_roots=(applications,),
        spotlight=lambda: (applications / "Adobe Lightroom.app",),
        disk_usage=_disk_usage,
        python_312_probe=lambda: False,
    )

    assert [(editor.editor, editor.application_path.name) for editor in discovery.editors] == [
        ("lightroom-cloud", "Adobe Lightroom.app"),
        ("photoshop-camera-raw", "Adobe Photoshop 2026.app"),
        ("lightcraft", "LightCraft.app"),
    ]
    assert all(not hasattr(editor, "status") for editor in discovery.editors)


def test_default_root_is_user_library_application_support_with_spaces(tmp_path: Path) -> None:
    assert default_install_root(tmp_path / "Дом Пользователя") == (
        tmp_path / "Дом Пользователя/Library/Application Support/Silverloupe"
    )


def test_discovery_marks_existing_install_only_for_valid_config(tmp_path: Path) -> None:
    install_root = tmp_path / "install"
    state = install_root / ".silverloupe"
    state.mkdir(parents=True)
    config = InstallConfig(
        hosts=["codex"],
        install_root=str(install_root.resolve()),
        enabled_editors=["report-only"],
        default_editor="report-only",
        integration_mode_by_editor={"report-only": "report-only"},
        runtime_version="0.1.0",
    )
    (state / "config.json").write_text(config.model_dump_json(), encoding="utf-8")

    valid = discover_install_environment(
        install_root,
        system="Darwin",
        machine="arm64",
        which=lambda _name: None,
        application_roots=(),
        spotlight=lambda: (),
        disk_usage=_disk_usage,
        python_312_probe=lambda: False,
    )
    assert valid.existing_config == config

    (state / "config.json").write_text("not-json", encoding="utf-8")
    invalid = discover_install_environment(
        install_root,
        system="Darwin",
        machine="arm64",
        which=lambda _name: None,
        application_roots=(),
        spotlight=lambda: (),
        disk_usage=_disk_usage,
        python_312_probe=lambda: False,
    )
    assert invalid.existing_config is None
    assert invalid.diagnostics == ("existing Silverloupe configuration is invalid",)


def _discovery(
    tmp_path: Path,
    *,
    agents: tuple[AgentInstallDiscovery, ...] = (),
    editors: tuple[EditorInstallDiscovery, ...] = (),
) -> InstallDiscovery:
    return InstallDiscovery(
        system="Darwin",
        machine="arm64",
        agents=agents,
        editors=editors,
        uv_path=tmp_path / "bin/uv",
        python_312_available=True,
        free_bytes=20 * 1024**3,
        install_root=default_install_root(tmp_path / "home"),
        existing_config=None,
    )


def test_zero_agents_is_a_hard_stop_with_no_questions(tmp_path: Path) -> None:
    with pytest.raises(InstallerPrerequisiteError, match="Codex or Claude Code"):
        questions_for(_discovery(tmp_path))


def test_one_agent_and_no_editor_needs_only_final_confirmation(tmp_path: Path) -> None:
    discovery = _discovery(
        tmp_path,
        agents=(AgentInstallDiscovery("codex", tmp_path / "bin/codex"),),
    )

    assert questions_for(discovery) == ("confirm",)


def test_two_agents_add_exactly_one_host_question(tmp_path: Path) -> None:
    discovery = _discovery(
        tmp_path,
        agents=(
            AgentInstallDiscovery("codex", tmp_path / "bin/codex"),
            AgentInstallDiscovery("claude-code", tmp_path / "bin/claude"),
        ),
    )

    assert questions_for(discovery) == ("hosts", "confirm")


def test_detected_editors_add_exactly_one_editor_question(tmp_path: Path) -> None:
    discovery = _discovery(
        tmp_path,
        agents=(AgentInstallDiscovery("codex", tmp_path / "bin/codex"),),
        editors=(
            EditorInstallDiscovery("lightcraft", tmp_path / "LightCraft.app"),
            EditorInstallDiscovery("lightroom-classic", tmp_path / "Lightroom Classic.app"),
        ),
    )

    assert questions_for(discovery) == ("editor", "confirm")


@pytest.mark.parametrize(
    ("editor", "expected_mode"),
    [
        ("lightcraft", "native"),
        ("lightroom-classic", "report-only"),
        ("lightroom-cloud", "report-only"),
        ("photoshop-camera-raw", "report-only"),
    ],
)
def test_lightcraft_maps_to_native_and_adobe_maps_to_report_only(
    tmp_path: Path,
    editor: str,
    expected_mode: str,
) -> None:
    discovery = _discovery(
        tmp_path,
        agents=(AgentInstallDiscovery("codex", tmp_path / "bin/codex"),),
        editors=(EditorInstallDiscovery(editor, tmp_path / f"{editor}.app"),),  # type: ignore[arg-type]
    )
    selection = WizardSelection(
        action="install",
        hosts=("codex",),
        editor=editor,  # type: ignore[arg-type]
        confirmed=True,
    )

    config = build_install_config(discovery, selection, "0.2.0")

    assert config.default_editor == editor
    assert config.integration_mode_by_editor == {editor: expected_mode}


def test_config_uses_fixed_user_defaults_and_raw_is_off(tmp_path: Path) -> None:
    discovery = _discovery(
        tmp_path,
        agents=(AgentInstallDiscovery("codex", tmp_path / "bin/codex"),),
    )
    selection = WizardSelection(
        action="install",
        hosts=("codex",),
        editor="report-only",
        confirmed=True,
    )

    config = build_install_config(discovery, selection, "0.2.0")

    assert config.scope == "user"
    assert config.install_root == str(discovery.install_root)
    assert config.model_mode == "host-vision"
    assert config.cache_quota == 5 * 1024**3
    assert config.worker_limit == 1
    assert config.install_raw_extra is False
    assert config.integration_mode_by_editor == {"report-only": "report-only"}


def test_invalid_or_disappeared_selected_agent_is_rejected(tmp_path: Path) -> None:
    discovery = _discovery(
        tmp_path,
        agents=(AgentInstallDiscovery("codex", tmp_path / "bin/codex"),),
    )
    selection = WizardSelection(
        action="install",
        hosts=("claude-code",),
        editor="report-only",
        confirmed=True,
    )

    with pytest.raises(InstallerPrerequisiteError, match="no longer available"):
        build_install_config(discovery, selection, "0.2.0")
