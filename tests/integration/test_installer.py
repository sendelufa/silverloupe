from __future__ import annotations

from pathlib import Path
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from typing import Any

import pytest
from pydantic import ValidationError

from silverloupe.cli import main as cli_main
from silverloupe.contracts import DeliveryPlan, DeliveryReceipt, InstallConfig, SessionConfig
import silverloupe.installer.lifecycle as lifecycle_module
from silverloupe.installer.capabilities import MetadataBackend, probe_install
from silverloupe.installer.config import configure_editor
from silverloupe.installer.discovery import (
    AgentInstallDiscovery,
    EditorInstallDiscovery,
    InstallDiscovery,
)
from silverloupe.installer.lifecycle import (
    install_release,
    load_installed_config,
    uninstall_release,
    update_release,
)
from tools.build_collection import PRODUCTION_SKILLS, build_release


ROOT = Path(__file__).parents[2]


class _ScriptedWizardIO:
    def __init__(
        self,
        *,
        one: list[str] | None = None,
        many: list[tuple[str, ...]] | None = None,
        confirmations: list[bool] | None = None,
    ) -> None:
        self.one = iter(one or [])
        self.many = iter(many or [])
        self.confirmations = iter(confirmations or [])
        self.questions: list[str] = []
        self.results: list[tuple[str, list[tuple[str, str, str]]]] = []

    def choose_one(
        self,
        title: str,
        _explanation: str,
        _options: Any,
        *,
        default: str,
    ) -> str:
        self.questions.append(title)
        return next(self.one, default)

    def choose_many(
        self,
        title: str,
        _explanation: str,
        _options: Any,
        *,
        defaults: Any,
    ) -> tuple[str, ...]:
        self.questions.append(title)
        return next(self.many, tuple(defaults))

    def confirm(self, title: str, _explanation: str, *, default: bool) -> bool:
        self.questions.append(title)
        return next(self.confirmations, default)

    def show_status(self, title: str, rows: Any) -> None:
        self.results.append((title, list(rows)))

    def show_result(self, title: str, rows: Any) -> None:
        self.results.append((title, list(rows)))


def _guided_discovery(
    tmp_path: Path,
    *,
    agents: tuple[AgentInstallDiscovery, ...],
    editors: tuple[EditorInstallDiscovery, ...] = (),
    existing_config: InstallConfig | None = None,
    install_root: Path | None = None,
) -> InstallDiscovery:
    return InstallDiscovery(
        system="Darwin",
        machine="arm64",
        agents=agents,
        editors=editors,
        uv_path=Path(shutil.which("uv") or sys.executable),
        python_312_available=True,
        free_bytes=20 * 1024**3,
        install_root=(install_root or tmp_path / "Library/Application Support/Silverloupe").resolve(),
        existing_config=existing_config,
    )


def _release_installer(tmp_path: Path, version: str = "0.1.0") -> tuple[Any, Path]:
    artifact = build_release(ROOT, tmp_path / f"dist-{version}", version)
    unpacked = tmp_path / f"unpacked-{version}"
    with tarfile.open(artifact, "r:gz") as handle:
        handle.extractall(unpacked, filter="data")
    release = unpacked / f"silverloupe-{version}"
    spec = importlib.util.spec_from_file_location(
        f"silverloupe_release_installer_{version.replace('.', '_')}_{id(tmp_path)}",
        release / "install.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    shutil.rmtree(release / "__pycache__", ignore_errors=True)
    return module, release


def _config(tmp_path: Path, **updates: Any) -> InstallConfig:
    values: dict[str, Any] = {
        "hosts": ["codex"],
        "scope": "user",
        "install_root": str(tmp_path / "Silverloupe Runtime"),
        "enabled_editors": ["report-only"],
        "default_editor": "report-only",
        "integration_mode_by_editor": {"report-only": "report-only"},
        "runtime_version": "0.1.0",
        "model_mode": "host-vision",
        "model_profile": {"profile_version": "host-v1"},
        "cache_quota": 5 * 1024**3,
        "worker_limit": 2,
    }
    values.update(updates)
    return InstallConfig(**values)


def test_install_config_supports_both_hosts_and_multiple_editors(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        hosts=["codex", "claude-code"],
        enabled_editors=["lightroom-classic", "lightcraft"],
        default_editor="lightcraft",
        integration_mode_by_editor={
            "lightroom-classic": "file",
            "lightcraft": "native",
        },
        metadata_mapping_by_editor={
            "lightcraft": {"top": "rating:5", "reject": "flag:reject"}
        },
    )

    assert config.config_version == 1
    assert config.hosts == ["codex", "claude-code"]
    assert config.default_editor == "lightcraft"
    assert config.metadata_mapping_by_editor["lightcraft"]["top"] == "rating:5"


def test_configure_editor_returns_new_config_and_normalizes_legacy_name(tmp_path: Path) -> None:
    original = _config(tmp_path)

    configured = configure_editor(original, "lightroom", "file")

    assert configured is not original
    assert original.default_editor == "report-only"
    assert original.enabled_editors == ["report-only"]
    assert configured.default_editor == "lightroom-classic"
    assert configured.enabled_editors == ["report-only", "lightroom-classic"]
    assert configured.integration_mode_by_editor["lightroom-classic"] == "file"


@pytest.mark.parametrize(
    ("editor", "mode"),
    [
        ("unknown", "native"),
        ("lightcraft", "file"),
        ("lightroom-cloud", "file"),
        ("report-only", "native"),
        ("lightroom-classic", "automatic"),
    ],
)
def test_configure_editor_rejects_unknown_or_implicit_routes(
    tmp_path: Path,
    editor: str,
    mode: str,
) -> None:
    with pytest.raises(ValueError):
        configure_editor(_config(tmp_path), editor, mode)


def test_install_config_rejects_inconsistent_or_unsafe_values(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _config(tmp_path, hosts=[])
    with pytest.raises(ValidationError):
        _config(tmp_path, hosts=["codex", "codex"])
    with pytest.raises(ValidationError):
        _config(tmp_path, default_editor="lightcraft")
    with pytest.raises(ValidationError):
        _config(tmp_path, integration_mode_by_editor={})
    with pytest.raises(ValidationError):
        _config(tmp_path, install_root="relative/path")
    with pytest.raises(ValidationError):
        _config(tmp_path, cache_quota=0)
    with pytest.raises(ValidationError):
        _config(tmp_path, worker_limit=0)
    with pytest.raises(ValidationError):
        _config(
            tmp_path,
            enabled_editors=["lightcraft"],
            default_editor="lightcraft",
            integration_mode_by_editor={"lightcraft": "file"},
        )


def test_missing_native_mcp_is_report_only_without_file_fallback(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        hosts=["codex", "claude-code"],
        enabled_editors=["lightcraft"],
        default_editor="lightcraft",
        integration_mode_by_editor={"lightcraft": "native"},
    )

    report = probe_install(
        config,
        probes={
            "platform": {"system": "Darwin", "machine": "arm64", "free_bytes": 20 * 1024**3},
            "hosts": {
                "codex": {"available": True, "vision": True},
                "claude-code": {"available": True, "vision": True},
            },
            "editors": {
                "lightcraft": {
                    "available": True,
                    "native_by_host": {"codex": True, "claude-code": False},
                    "fields": ["rating", "flag", "label"],
                }
            },
            "raw": {"editor_renderer": True, "wheel": False, "camera_supported": True},
        },
    )

    editor = report["editors"]["lightcraft"]
    assert editor["requested_mode"] == "native"
    assert editor["effective_mode"] == "report-only"
    assert editor["status"] == "degraded"
    assert editor["per_host"]["codex"]["status"] == "ready"
    assert editor["per_host"]["claude-code"]["status"] == "unavailable"
    assert "MCP" in editor["per_host"]["claude-code"]["reason"]


def test_explicit_file_mode_reports_xmp_fields_and_does_not_probe_native(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        enabled_editors=["lightroom-classic"],
        default_editor="lightroom-classic",
        integration_mode_by_editor={"lightroom-classic": "file"},
    )

    report = probe_install(
        config,
        probes={
            "platform": {"system": "Darwin", "machine": "arm64", "free_bytes": 20 * 1024**3},
            "hosts": {"codex": {"available": True, "vision": True}},
            "editors": {"lightroom-classic": {"available": False}},
            "raw": {"editor_renderer": False, "wheel": True, "camera_supported": True},
        },
    )

    editor = report["editors"]["lightroom-classic"]
    assert editor == {
        "effective_mode": "report-only",
        "fields": ["keywords", "label", "rating"],
        "requested_mode": "file",
        "reason": "editor_format_combination_not_verified",
        "status": "unavailable",
    }


def test_host_vision_uses_host_capability_without_local_model_weights(tmp_path: Path) -> None:
    report = probe_install(
        _config(tmp_path),
        probes={
            "platform": {"system": "Darwin", "machine": "arm64", "free_bytes": 20 * 1024**3},
            "hosts": {"codex": {"available": True, "vision": True}},
            "editors": {},
            "raw": {"editor_renderer": False, "wheel": False, "camera_supported": False},
        },
    )

    assert report["model"] == {
        "local_weights_required": False,
        "mode": "host-vision",
        "status": "ready",
    }
    assert report["raw"]["status"] == "unavailable"
    assert "RAW decoder" in report["raw"]["reason"]


class _Backend:
    def capabilities(self) -> dict[str, object]:
        return {"status": "ready"}

    def plan(self, *args: object, **kwargs: object) -> DeliveryPlan:
        return DeliveryPlan(target="fake", profile_version="1")

    def apply(self, plan: DeliveryPlan) -> DeliveryReceipt:
        return DeliveryReceipt(target=plan.target)

    def readback(self, receipt: DeliveryReceipt) -> dict[str, object]:
        return {"target": receipt.target}


def test_metadata_backend_protocol_covers_capabilities_plan_apply_and_readback() -> None:
    assert isinstance(_Backend(), MetadataBackend)


@pytest.mark.parametrize(
    "hosts",
    [["codex"], ["claude-code"], ["codex", "claude-code"]],
)
def test_clean_install_registers_exactly_six_skills_for_each_selected_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hosts: list[str],
) -> None:
    home = tmp_path / "Home With Spaces Ω"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    artifact = build_release(ROOT, tmp_path / "dist", "0.1.0")
    config = _config(tmp_path, hosts=hosts)

    result = install_release(artifact, config)

    assert result["status"] == "installed"
    assert result["active_version"] == "0.1.0"
    assert load_installed_config(Path(config.install_root)) == config
    assert (Path(config.install_root) / ".silverloupe/active-version").read_text() == "0.1.0\n"
    launcher = Path(config.install_root) / "bin/silverloupe"
    assert launcher.is_file()
    assert launcher.stat().st_mode & 0o111
    for host in hosts:
        host_root = home / (".agents" if host == "codex" else ".claude") / "skills"
        assert {path.name for path in host_root.iterdir()} == set(PRODUCTION_SKILLS)
        entry_skill = (host_root / "silverloupe/SKILL.md").read_text(encoding="utf-8")
        assert str(launcher) in entry_skill
    ownership = json.loads(
        (Path(config.install_root) / ".silverloupe/ownership.json").read_text()
    )
    assert ownership["active_version"] == "0.1.0"
    assert all(Path(entry["path"]).is_absolute() for entry in ownership["files"])


def test_reinstalling_same_release_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    artifact = build_release(ROOT, tmp_path / "dist", "0.1.0")
    config = _config(tmp_path)
    first = install_release(artifact, config)
    ownership_before = (Path(config.install_root) / ".silverloupe/ownership.json").read_bytes()

    second = install_release(artifact, config)

    assert first["status"] == "installed"
    assert second["status"] == "unchanged"
    assert (Path(config.install_root) / ".silverloupe/ownership.json").read_bytes() == ownership_before


def test_project_scope_registers_skills_inside_install_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    artifact = build_release(ROOT, tmp_path / "dist", "0.1.0")
    config = _config(tmp_path, hosts=["codex", "claude-code"], scope="project")

    install_release(artifact, config)

    install_root = Path(config.install_root)
    assert (install_root / ".agents/skills/silverloupe/SKILL.md").is_file()
    assert (install_root / ".claude/skills/silverloupe/SKILL.md").is_file()
    assert not (home / ".agents/skills/silverloupe").exists()
    assert not (home / ".claude/skills/silverloupe").exists()


def _rewrite_archive(archive: Path, destination: Path, mutate: str) -> Path:
    with tarfile.open(archive, "r:gz") as source, tarfile.open(destination, "w:gz") as target:
        for member in source.getmembers():
            data = source.extractfile(member).read() if member.isfile() else b""
            if member.name.endswith(mutate):
                data += b"tampered"
                member.size = len(data)
            target.addfile(member, io.BytesIO(data) if member.isfile() else None)
    return destination


def test_install_rejects_checksum_mismatch_before_writing_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    artifact = build_release(ROOT, tmp_path / "dist", "0.1.0")
    tampered = _rewrite_archive(artifact, tmp_path / "tampered.tar.gz", "USER_GUIDE.md")
    config = _config(tmp_path)

    with pytest.raises(ValueError, match="checksum"):
        install_release(tampered, config)

    assert not Path(config.install_root).exists()
    assert not (home / ".codex/skills/silverloupe").exists()


def test_install_rejects_archive_traversal_before_writing_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    archive = tmp_path / "malicious.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        payload = b"owned"
        member = tarfile.TarInfo("silverloupe-0.1.0/../../owned.txt")
        member.size = len(payload)
        handle.addfile(member, io.BytesIO(payload))
    config = _config(tmp_path)

    with pytest.raises(ValueError, match="unsafe archive path"):
        install_release(archive, config)

    assert not (tmp_path / "owned.txt").exists()
    assert not Path(config.install_root).exists()


def test_failed_update_keeps_previous_activation_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    first_artifact = build_release(ROOT, tmp_path / "dist-1", "0.1.0")
    second_artifact = build_release(ROOT, tmp_path / "dist-2", "0.2.0")
    first = _config(tmp_path)
    install_release(first_artifact, first)
    state_root = Path(first.install_root) / ".silverloupe"
    config_before = (state_root / "config.json").read_bytes()

    second = first.model_copy(update={"runtime_version": "0.2.0"})
    with pytest.raises(RuntimeError, match="injected update failure"):
        update_release(second_artifact, second, failpoint="after_stage")

    assert (state_root / "active-version").read_text() == "0.1.0\n"
    assert (state_root / "config.json").read_bytes() == config_before
    assert not list(Path(first.install_root).glob(".staging-*"))


def test_update_and_editor_change_preserve_existing_session_snapshot_and_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    first_artifact = build_release(ROOT, tmp_path / "dist-1", "0.1.0")
    second_artifact = build_release(ROOT, tmp_path / "dist-2", "0.2.0")
    first = _config(tmp_path)
    install_release(first_artifact, first)
    session_root = Path(first.install_root) / "sessions/existing"
    session_root.mkdir(parents=True)
    snapshot = b'{"editor_config_snapshot":{"default_editor":"report-only"}}\n'
    plan = b'{"target":"report-only","status":"report_only"}\n'
    (session_root / "session.json").write_bytes(snapshot)
    (session_root / "delivery-plan.json").write_bytes(plan)
    second = configure_editor(
        first.model_copy(update={"runtime_version": "0.2.0"}),
        "lightroom-classic",
        "file",
    )

    result = update_release(second_artifact, second)

    assert result["status"] == "updated"
    assert result["active_version"] == "0.2.0"
    assert load_installed_config(Path(first.install_root)).default_editor == "lightroom-classic"
    assert (session_root / "session.json").read_bytes() == snapshot
    assert (session_root / "delivery-plan.json").read_bytes() == plan


def test_uninstall_removes_only_owned_files_and_preserves_foreign_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    foreign = home / ".codex/skills/my-own-skill/SKILL.md"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("mine\n")
    monkeypatch.setenv("HOME", str(home))
    artifact = build_release(ROOT, tmp_path / "dist", "0.1.0")
    config = _config(tmp_path)
    install_release(artifact, config)
    session = Path(config.install_root) / "sessions/keep/result.json"
    session.parent.mkdir(parents=True)
    session.write_text("{}\n")
    external_photo = tmp_path / "photos/original.jpg"
    external_photo.parent.mkdir()
    external_photo.write_bytes(b"photo")

    result = uninstall_release(config)

    assert result["status"] == "uninstalled"
    assert foreign.read_text() == "mine\n"
    assert session.read_text() == "{}\n"
    assert external_photo.read_bytes() == b"photo"
    assert not (home / ".agents/skills/silverloupe").exists()
    assert not (Path(config.install_root) / ".silverloupe").exists()


def test_release_bootstrap_cli_installs_without_source_checkout_on_pythonpath(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    artifact = build_release(ROOT, tmp_path / "dist", "0.1.0")
    unpacked = tmp_path / "unpacked"
    with tarfile.open(artifact, "r:gz") as handle:
        handle.extractall(unpacked, filter="data")
    release = unpacked / "silverloupe-0.1.0"
    install_root = tmp_path / "Установка Silverloupe"
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "")}

    completed = subprocess.run(
        [
            sys.executable,
            str(release / "install.py"),
            "--host",
            "codex",
            "--editor",
            "report-only",
            "--mode",
            "report-only",
            "--install-root",
            str(install_root),
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout)["status"] == "installed"
    assert (install_root / ".silverloupe/active-version").read_text() == "0.1.0\n"


def test_bootstrap_verifies_release_before_importing_runtime_code(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    artifact = build_release(ROOT, tmp_path / "dist", "0.1.0")
    unpacked = tmp_path / "unpacked"
    with tarfile.open(artifact, "r:gz") as handle:
        handle.extractall(unpacked, filter="data")
    release = unpacked / "silverloupe-0.1.0"
    marker = tmp_path / "runtime-executed"
    runtime_init = release / "runtime/src/silverloupe/__init__.py"
    runtime_init.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
        + runtime_init.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(release / "install.py"),
            "--host",
            "codex",
            "--editor",
            "report-only",
            "--install-root",
            str(tmp_path / "install"),
        ],
        cwd=tmp_path,
        env={"HOME": str(home), "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "checksum" in completed.stderr
    assert not marker.exists()


def test_release_version_metadata_cannot_escape_versions_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    artifact = build_release(ROOT, tmp_path / "dist", "0.1.0")
    unpacked = tmp_path / "unpacked"
    with tarfile.open(artifact, "r:gz") as handle:
        handle.extractall(unpacked, filter="data")
    release = unpacked / "silverloupe-0.1.0"
    manifest_path = release / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["release_version"] = "../../escaped-release"
    manifest_path.write_text(json.dumps(manifest))
    config = _config(tmp_path).model_copy(update={"runtime_version": "../../escaped-release"})

    with pytest.raises(ValueError, match="version"):
        install_release(release, config)

    assert not (tmp_path / "escaped-release").exists()


def test_activation_write_failure_rolls_back_config_launcher_and_host_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    first_artifact = build_release(ROOT, tmp_path / "dist-1", "0.1.0")
    second_artifact = build_release(ROOT, tmp_path / "dist-2", "0.2.0")
    first = _config(tmp_path)
    install_release(first_artifact, first)
    install_root = Path(first.install_root)
    state_root = install_root / ".silverloupe"
    skill_path = home / ".agents/skills/silverloupe/SKILL.md"
    before = {
        "active": (state_root / "active-version").read_bytes(),
        "config": (state_root / "config.json").read_bytes(),
        "launcher": (install_root / "bin/silverloupe").read_bytes(),
        "skill": skill_path.read_bytes(),
    }
    real_atomic_write = lifecycle_module.atomic_write_text

    def fail_active(path: Path, text: str) -> None:
        if path.name == "active-version" and text == "0.2.0\n":
            raise OSError("injected active pointer failure")
        real_atomic_write(path, text)

    monkeypatch.setattr(lifecycle_module, "atomic_write_text", fail_active)
    second = configure_editor(
        first.model_copy(update={"runtime_version": "0.2.0"}),
        "lightroom-classic",
        "file",
    )

    with pytest.raises(OSError, match="active pointer"):
        update_release(second_artifact, second)

    assert (state_root / "active-version").read_bytes() == before["active"]
    assert (state_root / "config.json").read_bytes() == before["config"]
    assert (install_root / "bin/silverloupe").read_bytes() == before["launcher"]
    assert skill_path.read_bytes() == before["skill"]
    assert not (install_root / "versions/0.2.0").exists()


def test_uninstall_preserves_external_files_behind_replaced_parent_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    artifact = build_release(ROOT, tmp_path / "dist", "0.1.0")
    config = _config(tmp_path)
    install_release(artifact, config)
    installed_skill = home / ".agents/skills/silverloupe"
    external = tmp_path / "external-skill"
    shutil.copytree(installed_skill, external)
    shutil.rmtree(installed_skill)
    installed_skill.symlink_to(external, target_is_directory=True)

    result = uninstall_release(config)

    assert (external / "SKILL.md").is_file()
    assert result["preserved"]


def test_new_session_uses_installed_model_budget_and_editor_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    artifact = build_release(ROOT, tmp_path / "dist", "0.1.0")
    config = _config(
        tmp_path,
        enabled_editors=["lightcraft"],
        default_editor="lightcraft",
        integration_mode_by_editor={"lightcraft": "native"},
        model_mode="local-signals",
        cache_quota=123456,
        worker_limit=1,
    )
    install_release(artifact, config)
    monkeypatch.setenv("SILVERLOUPE_INSTALL_ROOT", config.install_root)
    source = ROOT / "evals/fixtures/files"
    output = tmp_path / "session"

    assert cli_main(["prepare", str(source), str(output)]) == 0

    session = SessionConfig.model_validate_json((output / "session.json").read_text())
    assert session.runtime_mode == "local"
    assert session.budget == {"cache_quota": 123456, "worker_limit": 1}
    assert session.editor_config_snapshot["default_editor"] == "lightcraft"
    assert session.editor_config_snapshot["integration_mode"] == "native"


def test_install_refuses_to_overwrite_unowned_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    artifact = build_release(ROOT, tmp_path / "dist", "0.1.0")
    config = _config(tmp_path)
    launcher = Path(config.install_root) / "bin/silverloupe"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("foreign\n")

    with pytest.raises(ValueError, match="launcher"):
        install_release(artifact, config)

    assert launcher.read_text() == "foreign\n"


def test_management_cli_reports_capabilities_configures_updates_and_uninstalls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    first_artifact = build_release(ROOT, tmp_path / "dist-1", "0.1.0")
    second_artifact = build_release(ROOT, tmp_path / "dist-2", "0.2.0")
    config = _config(tmp_path)
    install_release(first_artifact, config)
    monkeypatch.setenv("SILVERLOUPE_INSTALL_ROOT", config.install_root)

    assert cli_main(["doctor"]) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert "installation" in doctor
    assert cli_main(["configure", "--editor", "lightroom-classic", "--mode", "file"]) == 0
    capsys.readouterr()
    assert load_installed_config(Path(config.install_root)).default_editor == "lightroom-classic"
    assert cli_main(["update", str(second_artifact)]) == 0
    capsys.readouterr()
    assert load_installed_config(Path(config.install_root)).runtime_version == "0.2.0"
    assert cli_main(["uninstall"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "uninstalled"


def test_interactive_installer_stops_before_mutation_when_no_agent_exists(
    tmp_path: Path,
) -> None:
    module, _release = _release_installer(tmp_path)
    discovery = _guided_discovery(tmp_path, agents=())
    io_adapter = _ScriptedWizardIO()

    assert module.main([], discovery=lambda: discovery, io=io_adapter) == 2
    assert not discovery.install_root.exists()
    assert io_adapter.results[-1][0] == "Cannot install Silverloupe"
    assert "Codex or Claude Code" in io_adapter.results[-1][1][0][2]


def test_single_agent_no_editor_installs_report_only_without_extra_questions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    agent = tmp_path / "bin/codex"
    agent.parent.mkdir()
    agent.write_text("#!/bin/sh\n", encoding="utf-8")
    agent.chmod(0o755)
    module, _release = _release_installer(tmp_path)
    discovery = _guided_discovery(
        tmp_path,
        agents=(AgentInstallDiscovery("codex", agent.resolve()),),
    )
    io_adapter = _ScriptedWizardIO(confirmations=[True])

    assert module.main([], discovery=lambda: discovery, io=io_adapter) == 0

    installed = load_installed_config(discovery.install_root)
    assert installed.hosts == ["codex"]
    assert installed.default_editor == "report-only"
    assert io_adapter.questions == ["Install Silverloupe?"]


def test_two_agents_and_editors_use_selected_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    codex = tmp_path / "bin/codex"
    claude = tmp_path / "bin/claude"
    codex.parent.mkdir()
    for executable in (codex, claude):
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
    module, _release = _release_installer(tmp_path)
    discovery = _guided_discovery(
        tmp_path,
        agents=(
            AgentInstallDiscovery("codex", codex.resolve()),
            AgentInstallDiscovery("claude-code", claude.resolve()),
        ),
        editors=(
            EditorInstallDiscovery("lightroom-classic", tmp_path / "Lightroom Classic.app"),
            EditorInstallDiscovery("lightcraft", tmp_path / "LightCraft.app"),
        ),
    )
    io_adapter = _ScriptedWizardIO(
        many=[("codex", "claude-code")],
        one=["lightcraft"],
        confirmations=[True],
    )

    assert module.main([], discovery=lambda: discovery, io=io_adapter) == 0

    installed = load_installed_config(discovery.install_root)
    assert installed.hosts == ["codex", "claude-code"]
    assert installed.default_editor == "lightcraft"
    assert installed.integration_mode_by_editor == {"lightcraft": "native"}


def test_agent_disappearing_before_confirmation_aborts_before_install(
    tmp_path: Path,
) -> None:
    agent = tmp_path / "bin/codex"
    agent.parent.mkdir()
    agent.touch()
    before = _guided_discovery(
        tmp_path,
        agents=(AgentInstallDiscovery("codex", agent.resolve()),),
    )
    after = _guided_discovery(tmp_path, agents=(), install_root=before.install_root)
    values = iter((before, after))
    module, _release = _release_installer(tmp_path)
    io_adapter = _ScriptedWizardIO(confirmations=[True])

    assert module.main([], discovery=lambda: next(values), io=io_adapter) == 2
    assert not before.install_root.exists()
    assert "no longer available" in io_adapter.results[-1][1][0][2]


def test_default_install_root_with_spaces_and_unicode_installs_successfully(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "Дом Пользователя"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    agent = tmp_path / "bin/codex"
    agent.parent.mkdir()
    agent.touch()
    install_root = home / "Library/Application Support/Silverloupe"
    discovery = _guided_discovery(
        tmp_path,
        agents=(AgentInstallDiscovery("codex", agent.resolve()),),
        install_root=install_root,
    )
    module, _release = _release_installer(tmp_path)

    assert (
        module.main(
            [], discovery=lambda: discovery, io=_ScriptedWizardIO(confirmations=[True])
        )
        == 0
    )
    assert load_installed_config(install_root).install_root == str(install_root.resolve())


def test_non_interactive_requires_all_values_and_preserves_json_stdout(
    tmp_path: Path,
) -> None:
    _module, release = _release_installer(tmp_path)

    missing = subprocess.run(
        [sys.executable, str(release / "install.py"), "--non-interactive"],
        capture_output=True,
        text=True,
    )
    assert missing.returncode == 2
    assert "--host, --editor and --install-root" in missing.stderr

    install_root = tmp_path / "runtime"
    complete = subprocess.run(
        [
            sys.executable,
            str(release / "install.py"),
            "--non-interactive",
            "--host",
            "codex",
            "--editor",
            "report-only",
            "--mode",
            "report-only",
            "--install-root",
            str(install_root),
        ],
        capture_output=True,
        text=True,
        check=True,
        env={"HOME": str(tmp_path / "home"), "PATH": os.environ.get("PATH", "")},
    )
    assert json.loads(complete.stdout)["status"] == "installed"


def test_existing_install_offers_update_reconfigure_and_uninstall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    module, release = _release_installer(tmp_path)
    config = _config(tmp_path)
    install_release(release, config)
    agent = tmp_path / "bin/codex"
    agent.parent.mkdir()
    agent.touch()
    discovery = _guided_discovery(
        tmp_path,
        agents=(AgentInstallDiscovery("codex", agent.resolve()),),
        existing_config=config,
        install_root=Path(config.install_root),
    )
    io_adapter = _ScriptedWizardIO(one=["uninstall"], confirmations=[True])

    assert module.main([], discovery=lambda: discovery, io=io_adapter) == 0
    assert not (Path(config.install_root) / ".silverloupe/config.json").exists()
    assert io_adapter.questions == ["Existing installation", "Uninstall Silverloupe?"]


def test_success_runs_doctor_and_shows_first_agent_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    agent = tmp_path / "bin/codex"
    agent.parent.mkdir()
    agent.touch()
    discovery = _guided_discovery(
        tmp_path,
        agents=(AgentInstallDiscovery("codex", agent.resolve()),),
    )
    module, _release = _release_installer(tmp_path)
    io_adapter = _ScriptedWizardIO(confirmations=[True])

    assert module.main([], discovery=lambda: discovery, io=io_adapter) == 0

    title, rows = io_adapter.results[-1]
    assert title == "Silverloupe is ready"
    assert any(label == "Next step" and "Codex" in detail for label, _status, detail in rows)
