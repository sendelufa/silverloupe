"""Strict editor and integration configuration."""

from __future__ import annotations

from silverloupe.contracts import InstallConfig


_EDITOR_ALIASES = {"lightroom": "lightroom-classic"}
_EDITOR_MODES = {
    "lightroom-classic": {"native", "file", "report-only"},
    "lightroom-cloud": {"native", "report-only"},
    "photoshop-camera-raw": {"native", "file", "report-only"},
    "lightcraft": {"native", "report-only"},
    "report-only": {"report-only"},
}


def configure_editor(config: InstallConfig, editor: str, mode: str) -> InstallConfig:
    """Enable and select an editor without mutating the prior configuration."""

    normalized = _EDITOR_ALIASES.get(editor, editor)
    allowed = _EDITOR_MODES.get(normalized)
    if allowed is None:
        raise ValueError(f"unknown editor: {editor}")
    if mode not in allowed:
        raise ValueError(f"integration mode {mode!r} is not valid for {normalized}")

    enabled = list(config.enabled_editors)
    if normalized not in enabled:
        enabled.append(normalized)  # type: ignore[arg-type]
    modes = dict(config.integration_mode_by_editor)
    modes[normalized] = mode  # type: ignore[assignment]
    return config.model_copy(
        update={
            "enabled_editors": enabled,
            "default_editor": normalized,
            "integration_mode_by_editor": modes,
        },
        deep=True,
    )
