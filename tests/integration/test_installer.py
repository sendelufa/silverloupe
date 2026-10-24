from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from silverloupe.contracts import DeliveryPlan, DeliveryReceipt, InstallConfig
from silverloupe.installer.capabilities import MetadataBackend, probe_install
from silverloupe.installer.config import configure_editor


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
