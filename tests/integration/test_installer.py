from __future__ import annotations

from pathlib import Path
import io
import json
import os
import subprocess
import sys
import tarfile
from typing import Any

import pytest
from pydantic import ValidationError

from silverloupe.contracts import DeliveryPlan, DeliveryReceipt, InstallConfig
from silverloupe.installer.capabilities import MetadataBackend, probe_install
from silverloupe.installer.config import configure_editor
from silverloupe.installer.lifecycle import (
    install_release,
    load_installed_config,
    uninstall_release,
    update_release,
)
from tools.build_collection import PRODUCTION_SKILLS, build_release


ROOT = Path(__file__).parents[2]


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
        "effective_mode": "file",
        "fields": ["keywords", "label", "rating"],
        "requested_mode": "file",
        "status": "ready",
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
        host_root = home / (".codex" if host == "codex" else ".claude") / "skills"
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
    assert (install_root / ".codex/skills/silverloupe/SKILL.md").is_file()
    assert (install_root / ".claude/skills/silverloupe/SKILL.md").is_file()
    assert not (home / ".codex/skills/silverloupe").exists()
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
    assert not (home / ".codex/skills/silverloupe").exists()
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
