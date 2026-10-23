"""Conservative local image signals for technical triage."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from silverloupe.contracts import Asset, Brief, RunConfig


_MIN_REVIEW_EDGE = 512


@dataclass(frozen=True)
class SignalResult:
    evaluation_status: str
    technical_signals: dict[str, Any]
    uncertainty: list[str]


def _preview_path(asset: Asset, config: RunConfig) -> Path | None:
    provenance = asset.preview_provenance or {}
    relative_value = provenance.get("path")
    if not isinstance(relative_value, str):
        return None
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    output = Path(config.output_root).expanduser().resolve()
    candidate = output / relative
    if candidate.is_symlink() or not candidate.resolve().is_relative_to(output):
        return None
    return candidate if candidate.is_file() else None


def _file_sha256(path: Path | None) -> str | None:
    if path is None:
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def asset_input_fingerprint(asset: Asset, brief: Brief, config: RunConfig) -> str:
    """Bind assessment evidence to source identity, rendered bytes, policy, and brief."""

    identity = {
        "asset_id": asset.asset_id,
        "relative_path": asset.relative_path,
        "content_sha256": asset.content_sha256,
        "media_type": asset.media_type,
        "decode_status": asset.decode_status,
        "dimensions": asset.dimensions,
        "preview_provenance": asset.preview_provenance,
        "preview_sha256": _file_sha256(_preview_path(asset, config)),
        "rendering_policy": config.rendering_policy,
        "brief": brief.model_dump(mode="json"),
    }
    return hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _array_signals(image: Image.Image) -> dict[str, float]:
    gray = ImageOps.grayscale(image)
    gray.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
    pixels = np.asarray(gray, dtype=np.float32) / 255.0
    if pixels.ndim != 2 or pixels.size == 0:
        raise ValueError("preview has no measurable pixels")

    if min(pixels.shape) >= 3:
        center = pixels[1:-1, 1:-1]
        laplacian = (
            pixels[:-2, 1:-1]
            + pixels[2:, 1:-1]
            + pixels[1:-1, :-2]
            + pixels[1:-1, 2:]
            - (4.0 * center)
        )
        laplacian_variance = float(np.var(laplacian) * (255.0**2))
        high_frequency_energy = float(np.mean(np.abs(laplacian)))
    else:
        laplacian_variance = 0.0
        high_frequency_energy = 0.0

    return {
        "luminance_mean": round(float(np.mean(pixels)), 6),
        "dark_fraction": round(float(np.mean(pixels <= (10.0 / 255.0))), 6),
        "highlight_fraction": round(float(np.mean(pixels >= (245.0 / 255.0))), 6),
        "laplacian_variance": round(laplacian_variance, 6),
        "high_frequency_energy": round(high_frequency_energy, 6),
    }


def measure_asset_signals(asset: Asset, brief: Brief, config: RunConfig) -> SignalResult:
    """Measure reproducible preview-level signals without turning them into a verdict."""

    signals: dict[str, Any] = {
        "decode_status": asset.decode_status,
        "input_fingerprint": asset_input_fingerprint(asset, brief, config),
    }
    uncertainty: list[str] = []
    if asset.decode_status not in {"ready", "degraded"}:
        return SignalResult(
            evaluation_status="unassessed",
            technical_signals=signals,
            uncertainty=["decode_unavailable"],
        )

    preview = _preview_path(asset, config)
    if preview is None:
        return SignalResult(
            evaluation_status="unassessed",
            technical_signals=signals,
            uncertainty=["preview_unavailable"],
        )

    try:
        with Image.open(preview) as opened:
            opened.load()
            width, height = opened.size
            measured = _array_signals(opened)
    except (Image.DecompressionBombError, OSError, UnidentifiedImageError, ValueError):
        return SignalResult(
            evaluation_status="unassessed",
            technical_signals=signals,
            uncertainty=["preview_unreadable"],
        )

    signals["preview"] = {
        "path": str(preview.relative_to(Path(config.output_root).expanduser().resolve())),
        "width": width,
        "height": height,
        "scale": "overview",
    }
    signals.update(measured)

    if max(width, height) < _MIN_REVIEW_EDGE:
        uncertainty.append("preview_too_small")
    if measured["luminance_mean"] < 0.12 or measured["dark_fraction"] > 0.65:
        uncertainty.append("low_key_or_underexposure")
    if measured["highlight_fraction"] > 0.35:
        uncertainty.append("high_key_or_overexposure")
    if measured["laplacian_variance"] < 35.0:
        uncertainty.append("blur_or_soft_focus_possible")
    if measured["high_frequency_energy"] > 0.35:
        uncertainty.append("noise_or_detail_ambiguous")
    if brief.allowed_artistic_techniques and uncertainty:
        uncertainty.append("artistic_intent_possible")
    if asset.decode_status == "degraded":
        uncertainty.append("degraded_decode")

    return SignalResult(
        evaluation_status="assessed",
        technical_signals=signals,
        uncertainty=sorted(set(uncertainty)),
    )
