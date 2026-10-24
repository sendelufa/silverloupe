"""Explicit Adobe delivery target routing.

Application installation alone is not a write capability.  Profiles that name
an editor remain report-only until their exact compatibility row is verified.
The generic ``xmp-sidecar`` target is the low-level, contract-tested file
backend and makes no claim that an Adobe version has consumed the result.
"""

from __future__ import annotations

from dataclasses import dataclass


PROPRIETARY_RAW_EXTENSIONS = {
    ".3fr",
    ".arw",
    ".cr2",
    ".cr3",
    ".erf",
    ".fff",
    ".iiq",
    ".kdc",
    ".mef",
    ".mos",
    ".mrw",
    ".nef",
    ".nrw",
    ".orf",
    ".pef",
    ".raf",
    ".raw",
    ".rw2",
    ".rwl",
    ".sr2",
    ".srf",
    ".srw",
    ".x3f",
}


@dataclass(frozen=True)
class AdobeTarget:
    name: str
    backend: str
    verified: bool
    reason: str | None = None


_TARGETS = {
    "xmp-sidecar": AdobeTarget("xmp-sidecar", "xmp", True),
    "lightroom-classic-xmp": AdobeTarget(
        "lightroom-classic-xmp",
        "xmp",
        False,
        "editor_format_combination_not_verified",
    ),
    "photoshop-camera-raw-xmp": AdobeTarget(
        "photoshop-camera-raw-xmp",
        "xmp",
        False,
        "editor_format_combination_not_verified",
    ),
    "lightroom-classic-native": AdobeTarget(
        "lightroom-classic-native",
        "native",
        False,
        "native_capability_unavailable",
    ),
    "photoshop-native": AdobeTarget(
        "photoshop-native",
        "native",
        False,
        "native_capability_unavailable",
    ),
}


def target_profile(target: str) -> AdobeTarget:
    try:
        return _TARGETS[target]
    except KeyError as error:
        raise ValueError(f"unknown delivery target: {target}") from error
