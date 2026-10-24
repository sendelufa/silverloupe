"""Production installation, configuration, and capability inspection."""

from silverloupe.installer.capabilities import MetadataBackend, probe_install
from silverloupe.installer.config import configure_editor
from silverloupe.installer.lifecycle import (
    install_release,
    load_installed_config,
    uninstall_release,
    update_release,
)

__all__ = [
    "MetadataBackend",
    "configure_editor",
    "install_release",
    "load_installed_config",
    "probe_install",
    "uninstall_release",
    "update_release",
]
