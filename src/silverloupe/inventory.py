"""Read-only source inventory, preparation, and resumable preview generation."""

from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image, UnidentifiedImageError

from silverloupe.contracts import Asset, Manifest, RunConfig
from silverloupe.previews import RAW_EXTENSIONS, PreviewUnavailable, build_contact_sheets, render_preview
from silverloupe.state import CheckpointStore, atomic_write_text, load_run_config, write_session


RASTER_EXTENSIONS = {".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
IMAGE_EXTENSIONS = RASTER_EXTENSIONS | RAW_EXTENSIONS


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _asset_id(source: Path, relative_path: str) -> str:
    value = f"{source}\0{relative_path}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:24]


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)


def _inspect_raster(path: Path) -> tuple[dict[str, Any], tuple[int, int] | None, str, dict[str, Any] | None]:
    exif_values: dict[str, Any] = {}
    try:
        with Image.open(path) as image:
            dimensions = image.size
            try:
                exif_values = {
                    ExifTags.TAGS.get(key, str(key)): _json_value(value)
                    for key, value in image.getexif().items()
                }
            except (OSError, TypeError, ValueError) as error:
                exif_values = {"_error": f"invalid_exif:{type(error).__name__}"}
            image.load()
        return exif_values, dimensions, "ready", None
    except (OSError, UnidentifiedImageError) as error:
        return (
            {"_error": f"decode_error:{type(error).__name__}"},
            None,
            "error",
            {"status": "unavailable", "reason": f"decode_error:{type(error).__name__}"},
        )


def _is_inside(path: Path, directory: Path) -> bool:
    return path == directory or path.is_relative_to(directory)


def build_inventory(source: Path, output: Path) -> Manifest:
    """Recursively inventory supported image paths without mutating the source."""

    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"source directory does not exist: {source}")
    if source == output:
        raise ValueError("output directory must differ from source directory")

    candidates: list[Path] = []
    for path in source.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        resolved = path.resolve()
        if _is_inside(resolved, output):
            continue
        if path.suffix.lower() in IMAGE_EXTENSIONS:
            candidates.append(path)

    assets: list[Asset] = []
    for path in sorted(candidates, key=lambda item: item.relative_to(source).as_posix()):
        relative_path = path.relative_to(source).as_posix()
        suffix = path.suffix.lower()
        if suffix in RAW_EXTENSIONS:
            exif_values: dict[str, Any] = {}
            dimensions = None
            decode_status = "unassessed"
            provenance = None
            media_type = "image/x-raw"
        else:
            exif_values, dimensions, decode_status, provenance = _inspect_raster(path)
            media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        assets.append(
            Asset(
                asset_id=_asset_id(source, relative_path),
                relative_path=relative_path,
                content_sha256=_sha256(path),
                media_type=media_type,
                exif=exif_values,
                dimensions=dimensions,
                decode_status=decode_status,
                preview_provenance=provenance,
            )
        )
    return Manifest(assets=assets)


def _write_assets(output: Path, assets: list[Asset]) -> None:
    body = "".join(json.dumps(asset.model_dump(mode="json"), ensure_ascii=False, sort_keys=True) + "\n" for asset in assets)
    atomic_write_text(output / "assets.jsonl", body)


def _current_assets(original: list[Asset], processed: dict[str, Asset]) -> list[Asset]:
    return [processed.get(asset.asset_id, asset) for asset in original]


def prepare(source: Path, output: Path, config: RunConfig) -> Manifest:
    """Inventory a source and create resumable previews and contact sheets."""

    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    normalized_config = config.model_copy(
        update={"source_roots": [str(source)], "output_root": str(output)}
    )
    session_id = hashlib.sha256(f"{source}\0{output}".encode("utf-8")).hexdigest()[:20]
    write_session(output, normalized_config, session_id)
    inventory = build_inventory(source, output)
    store = CheckpointStore(output)
    processed: dict[str, Asset] = {}
    store.set_status("partial", len(inventory.assets), 0)

    try:
        for asset in inventory.assets:
            cached = store.cached(asset)
            if cached is not None:
                processed[asset.asset_id] = cached
                store.set_status("partial", len(inventory.assets), len(processed))
                continue

            if asset.decode_status == "error":
                prepared = asset
            else:
                try:
                    preview_path = render_preview(asset, normalized_config)
                    with Image.open(preview_path) as preview:
                        preview_size = preview.size
                    original_orientation = asset.exif.get("Orientation")
                    prepared = asset.model_copy(
                        update={
                            "decode_status": "ready",
                            "preview_provenance": {
                                "status": "ready",
                                "path": preview_path.relative_to(output).as_posix(),
                                "renderer": (
                                    "rawpy" if Path(asset.relative_path).suffix.lower() in RAW_EXTENSIONS else "pillow"
                                ),
                                "content_sha256": asset.content_sha256,
                                "max_edge": int(normalized_config.rendering_policy.get("max_edge", 1600)),
                                "dimensions": list(preview_size),
                                "orientation_normalized": True,
                                "original_orientation": original_orientation,
                            },
                        }
                    )
                except PreviewUnavailable as error:
                    prepared = asset.model_copy(
                        update={
                            "decode_status": error.decode_status,
                            "preview_provenance": {
                                "status": "unavailable",
                                "reason": error.reason,
                                "content_sha256": asset.content_sha256,
                            },
                        }
                    )

            store.record(prepared)
            processed[asset.asset_id] = prepared
            current = _current_assets(inventory.assets, processed)
            _write_assets(output, current)
            store.set_status("partial", len(inventory.assets), len(processed))

        final_assets = _current_assets(inventory.assets, processed)
        _write_assets(output, final_assets)
        build_contact_sheets(final_assets, output)
        store.set_status("completed", len(final_assets), len(final_assets))
        return Manifest(assets=final_assets)
    except BaseException as error:
        _write_assets(output, _current_assets(inventory.assets, processed))
        store.set_status("partial", len(inventory.assets), len(processed), f"{type(error).__name__}: {error}")
        raise
    finally:
        store.close()


def resume(output: Path) -> Manifest:
    output = output.expanduser().resolve()
    config = load_run_config(output)
    if len(config.source_roots) != 1:
        raise ValueError("resume currently requires exactly one source root")
    return prepare(Path(config.source_roots[0]), output, config)
