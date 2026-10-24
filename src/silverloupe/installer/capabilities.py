"""Non-mutating production capability probes and backend protocol."""

from __future__ import annotations

import importlib.util
import platform
import shutil
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from silverloupe.contracts import DeliveryPlan, DeliveryReceipt, InstallConfig


_FILE_FIELDS = {
    "lightroom-classic": ["keywords", "label", "rating"],
    "photoshop-camera-raw": ["keywords", "label", "rating"],
}


@runtime_checkable
class MetadataBackend(Protocol):
    """Common lifecycle required from every metadata writer."""

    def capabilities(self) -> Mapping[str, object]: ...

    def plan(self, *args: object, **kwargs: object) -> DeliveryPlan: ...

    def apply(self, plan: DeliveryPlan) -> DeliveryReceipt: ...

    def readback(self, receipt: DeliveryReceipt) -> Mapping[str, object]: ...


def _existing_parent(path: Path) -> Path:
    candidate = path.expanduser()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _default_probes(config: InstallConfig) -> dict[str, object]:
    root = _existing_parent(Path(config.install_root))
    return {
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "free_bytes": shutil.disk_usage(root).free,
        },
        "hosts": {
            host: {
                "available": shutil.which("codex" if host == "codex" else "claude")
                is not None,
                "vision": False,
            }
            for host in config.hosts
        },
        "editors": {},
        "raw": {
            "editor_renderer": False,
            "wheel": importlib.util.find_spec("rawpy") is not None,
            "camera_supported": False,
        },
    }


def _host_report(config: InstallConfig, values: Mapping[str, object]) -> dict[str, Any]:
    reports: dict[str, Any] = {}
    for host in config.hosts:
        raw = values.get(host, {})
        details = raw if isinstance(raw, Mapping) else {}
        available = details.get("available") is True
        vision = details.get("vision") is True
        if not available:
            reports[host] = {"status": "unavailable", "reason": "host executable unavailable"}
        elif config.model_mode == "host-vision" and not vision:
            reports[host] = {"status": "degraded", "reason": "host vision unavailable"}
        else:
            reports[host] = {"status": "ready"}
    return reports


def _editor_report(
    config: InstallConfig,
    editor: str,
    values: Mapping[str, object],
) -> dict[str, Any]:
    requested = config.integration_mode_by_editor[editor]
    if requested == "report-only" or editor == "report-only":
        return {
            "requested_mode": requested,
            "effective_mode": "report-only",
            "status": "ready",
            "fields": [],
        }
    if requested == "file":
        return {
            "requested_mode": "file",
            "effective_mode": "file",
            "status": "ready",
            "fields": _FILE_FIELDS[editor],
        }

    raw = values.get(editor, {})
    details = raw if isinstance(raw, Mapping) else {}
    native_by_host = details.get("native_by_host", {})
    host_values = native_by_host if isinstance(native_by_host, Mapping) else {}
    per_host: dict[str, dict[str, str]] = {}
    for host in config.hosts:
        if details.get("available") is True and host_values.get(host) is True:
            per_host[host] = {"status": "ready"}
        else:
            per_host[host] = {
                "status": "unavailable",
                "reason": f"native MCP/integration unavailable for {host}",
            }
    ready = sum(value["status"] == "ready" for value in per_host.values())
    if ready == len(per_host):
        status = "ready"
        effective = "native"
    elif ready:
        status = "degraded"
        effective = "report-only"
    else:
        status = "unavailable"
        effective = "report-only"
    fields = details.get("fields", [])
    return {
        "requested_mode": "native",
        "effective_mode": effective,
        "status": status,
        "fields": sorted(value for value in fields if isinstance(value, str)),
        "per_host": per_host,
    }


def _raw_report(values: Mapping[str, object]) -> dict[str, str]:
    renderer = values.get("editor_renderer") is True
    wheel = values.get("wheel") is True
    camera = values.get("camera_supported") is True
    if (renderer or wheel) and camera:
        return {
            "status": "ready",
            "backend": "editor-renderer" if renderer else "rawpy",
        }
    if renderer or wheel:
        return {
            "status": "degraded",
            "reason": "RAW decoder is present but camera support is unverified",
        }
    return {
        "status": "unavailable",
        "reason": "RAW decoder or verified editor renderer is unavailable",
    }


def probe_install(
    config: InstallConfig,
    *,
    probes: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Report selected production capabilities without changing user state."""

    observed = dict(_default_probes(config) if probes is None else probes)
    platform_values = observed.get("platform", {})
    platform_probe = platform_values if isinstance(platform_values, Mapping) else {}
    platform_ready = (
        platform_probe.get("system") == "Darwin"
        and platform_probe.get("machine") in {"arm64", "aarch64"}
    )
    free_bytes = platform_probe.get("free_bytes")
    enough_disk = isinstance(free_bytes, int) and free_bytes >= 5 * 1024**3
    platform_report = {
        "status": "ready" if platform_ready and enough_disk else "unavailable",
        "system": platform_probe.get("system"),
        "machine": platform_probe.get("machine"),
        "free_bytes": free_bytes,
    }

    host_values = observed.get("hosts", {})
    hosts = _host_report(
        config, host_values if isinstance(host_values, Mapping) else {}
    )
    editor_values = observed.get("editors", {})
    editor_probes = editor_values if isinstance(editor_values, Mapping) else {}
    editors = {
        editor: _editor_report(config, editor, editor_probes)
        for editor in config.enabled_editors
    }
    raw_values = observed.get("raw", {})
    raw_report = _raw_report(raw_values if isinstance(raw_values, Mapping) else {})

    if config.model_mode == "host-vision":
        ready_hosts = sum(value["status"] == "ready" for value in hosts.values())
        if ready_hosts == len(hosts):
            model_status = "ready"
        elif ready_hosts:
            model_status = "degraded"
        else:
            model_status = "unavailable"
        model_report = {
            "mode": "host-vision",
            "status": model_status,
            "local_weights_required": False,
        }
    else:
        model_report = {
            "mode": config.model_mode,
            "status": "unavailable",
            "local_weights_required": config.model_mode == "local-vision",
            "reason": "model profile has not passed release qualification",
        }

    statuses = [platform_report["status"], model_report["status"]]
    statuses.extend(value["status"] for value in hosts.values())
    statuses.extend(value["status"] for value in editors.values())
    overall = (
        "unavailable"
        if all(value == "unavailable" for value in statuses)
        else "degraded"
        if any(value != "ready" for value in statuses)
        else "ready"
    )
    return {
        "status": overall,
        "platform": platform_report,
        "hosts": hosts,
        "editors": editors,
        "model": model_report,
        "raw": raw_report,
    }
