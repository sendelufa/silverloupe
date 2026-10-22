"""Read-only source inventory, preparation, and resumable preview generation."""

from __future__ import annotations

import hashlib
import json
import mimetypes
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image, UnidentifiedImageError

from silverloupe.contracts import Asset, Manifest, RunConfig
from silverloupe.previews import (
    RAW_EXTENSIONS,
    PreviewUnavailable,
    build_contact_sheets,
    preview_render_details,
    remove_preview_artifacts,
    render_preview,
    renderer_cache_key,
    validate_output_layout,
)
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


def _extract_exif(image: Image.Image) -> dict[str, Any]:
    values: dict[str, Any] = {}
    exif = image.getexif()
    for key, value in exif.items():
        values[ExifTags.TAGS.get(key, str(key))] = _json_value(value)
    for ifd_name in ("Exif", "GPSInfo", "Interop"):
        ifd = getattr(ExifTags.IFD, ifd_name, None)
        if ifd is None:
            continue
        try:
            nested = exif.get_ifd(ifd)
        except (KeyError, OSError, TypeError, ValueError):
            continue
        for key, value in nested.items():
            tag_name = ExifTags.GPSTAGS.get(key, str(key)) if ifd_name == "GPSInfo" else ExifTags.TAGS.get(key, str(key))
            values[tag_name] = _json_value(value)
    return values


def _inspect_raster(path: Path) -> tuple[dict[str, Any], tuple[int, int] | None, str, dict[str, Any] | None]:
    try:
        with Image.open(path) as image:
            dimensions = image.size
            try:
                exif_values = _extract_exif(image)
            except (OSError, TypeError, ValueError) as error:
                exif_values = {"_error": f"invalid_exif:{type(error).__name__}"}
            image.load()
        return exif_values, dimensions, "ready", None
    except (Image.DecompressionBombError, OSError, UnidentifiedImageError) as error:
        return (
            {"_error": f"decode_error:{type(error).__name__}"},
            None,
            "error",
            {"status": "unavailable", "reason": f"decode_error:{type(error).__name__}"},
        )


def _raw_dimensions(raw: Any) -> tuple[int, int] | None:
    sizes = getattr(raw, "sizes", None)
    if sizes is None:
        return None
    width = getattr(sizes, "width", None) or getattr(sizes, "raw_width", None)
    height = getattr(sizes, "height", None) or getattr(sizes, "raw_height", None)
    return (int(width), int(height)) if width and height else None


def _inspect_raw(path: Path) -> tuple[dict[str, Any], tuple[int, int] | None]:
    try:
        import rawpy
    except ImportError:
        return {"_metadata_status": "unavailable:raw_backend_unavailable"}, None

    try:
        with rawpy.imread(str(path)) as raw:
            dimensions = _raw_dimensions(raw)
            try:
                thumb = raw.extract_thumb()
            except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
                return {"_metadata_status": "partial:no_embedded_thumbnail"}, dimensions
            if thumb.format == rawpy.ThumbFormat.JPEG:
                with Image.open(BytesIO(thumb.data)) as image:
                    values = _extract_exif(image)
                    dimensions = dimensions or image.size
                values.setdefault("_metadata_status", "embedded_thumbnail")
                return values, dimensions
            if thumb.format == rawpy.ThumbFormat.BITMAP:
                shape = getattr(thumb.data, "shape", ())
                if dimensions is None and len(shape) >= 2:
                    dimensions = (int(shape[1]), int(shape[0]))
                return {"_metadata_status": "partial:embedded_bitmap"}, dimensions
            return {"_metadata_status": "unavailable:unsupported_thumbnail"}, dimensions
    except Exception as error:
        return {"_metadata_status": f"unavailable:{type(error).__name__}"}, None


def _is_inside(path: Path, directory: Path) -> bool:
    return path == directory or path.is_relative_to(directory)


def _validated_roots(source: Path, output: Path) -> tuple[Path, Path]:
    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"source directory does not exist: {source}")
    if source == output:
        raise ValueError("output directory must differ from source directory")
    if source.is_relative_to(output):
        raise ValueError("output directory must not be an ancestor of the source directory")
    return source, output


def _file_signature(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _inventory_asset(source: Path, path: Path) -> Asset:
    relative_path = path.relative_to(source).as_posix()
    suffix = path.suffix.lower()
    last_values: tuple[dict[str, Any], tuple[int, int] | None, str, dict[str, Any] | None, str] | None = None
    stable = False
    for _attempt in range(2):
        before = _file_signature(path)
        if suffix in RAW_EXTENSIONS:
            exif_values, dimensions = _inspect_raw(path)
            decode_status = "unassessed"
            provenance = None
            media_type = "image/x-raw"
        else:
            exif_values, dimensions, decode_status, provenance = _inspect_raster(path)
            media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        digest = _sha256(path)
        after = _file_signature(path)
        last_values = (exif_values, dimensions, decode_status, provenance, digest)
        if before == after:
            stable = True
            break
    assert last_values is not None
    exif_values, dimensions, decode_status, provenance, digest = last_values
    if not stable:
        decode_status = "degraded"
        provenance = {"status": "unavailable", "reason": "source_changed_during_inventory"}
    return Asset(
        asset_id=_asset_id(source, relative_path),
        relative_path=relative_path,
        content_sha256=digest,
        media_type=media_type,
        exif=exif_values,
        dimensions=dimensions,
        decode_status=decode_status,
        preview_provenance=provenance,
    )


def build_inventory(source: Path, output: Path) -> Manifest:
    """Recursively inventory supported image paths without mutating the source."""

    source, output = _validated_roots(source, output)
    candidates: list[Path] = []
    for path in source.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        resolved = path.resolve()
        if _is_inside(resolved, output):
            continue
        if path.suffix.lower() in IMAGE_EXTENSIONS:
            candidates.append(path)

    assets = [
        _inventory_asset(source, path)
        for path in sorted(candidates, key=lambda item: item.relative_to(source).as_posix())
    ]
    return Manifest(assets=assets)


def _write_assets(output: Path, assets: list[Asset]) -> None:
    body = "".join(json.dumps(asset.model_dump(mode="json"), ensure_ascii=False, sort_keys=True) + "\n" for asset in assets)
    atomic_write_text(output / "assets.jsonl", body)


def _current_assets(original: list[Asset], processed: dict[str, Asset]) -> list[Asset]:
    return [processed.get(asset.asset_id, asset) for asset in original]


def _source_matches(source: Path, asset: Asset) -> bool:
    candidate = (source / asset.relative_path).resolve()
    return candidate.is_relative_to(source) and candidate.is_file() and _sha256(candidate) == asset.content_sha256


def prepare(source: Path, output: Path, config: RunConfig) -> Manifest:
    """Inventory a source and create resumable previews and contact sheets."""

    source, output = _validated_roots(source, output)
    validate_output_layout(output)
    output.mkdir(parents=True, exist_ok=True)
    validate_output_layout(output)
    normalized_config = config.model_copy(update={"source_roots": [str(source)], "output_root": str(output)})
    session_id = hashlib.sha256(f"{source}\0{output}".encode("utf-8")).hexdigest()[:20]
    store = CheckpointStore(output)
    processed: dict[str, Asset] = {}
    inventory: Manifest | None = None
    store.set_status("partial", 0, 0, "scanning")

    try:
        inventory = build_inventory(source, output)
        write_session(output, normalized_config, session_id)
        store.set_status("partial", len(inventory.assets), 0)
        for asset in inventory.assets:
            cache_key = renderer_cache_key(asset, normalized_config)
            cached = store.cached(asset, cache_key)
            if cached is not None:
                processed[asset.asset_id] = cached
                store.set_status("partial", len(inventory.assets), len(processed))
                continue

            if asset.decode_status == "error":
                prepared = asset
            else:
                preview_path: Path | None = None
                try:
                    if not _source_matches(source, asset):
                        raise PreviewUnavailable("degraded", "source_changed_during_prepare")
                    preview_path = render_preview(asset, normalized_config)
                    if not _source_matches(source, asset):
                        remove_preview_artifacts(preview_path, output)
                        raise PreviewUnavailable("degraded", "source_changed_during_prepare")
                    with Image.open(preview_path) as preview:
                        preview_size = preview.size
                    details = preview_render_details(preview_path)
                    prepared = asset.model_copy(
                        update={
                            "dimensions": asset.dimensions or preview_size,
                            "decode_status": "ready",
                            "preview_provenance": {
                                "status": "ready",
                                "path": preview_path.relative_to(output).as_posix(),
                                "renderer": details.get("renderer", "unknown"),
                                "render_source": details.get("render_source", "unknown"),
                                "content_sha256": asset.content_sha256,
                                "cache_key": cache_key,
                                "max_edge": int(normalized_config.rendering_policy.get("max_edge", 1600)),
                                "dimensions": list(preview_size),
                                "orientation_normalized": bool(details.get("orientation_normalized", False)),
                                "original_orientation": details.get("original_orientation"),
                            },
                        }
                    )
                except PreviewUnavailable as error:
                    if preview_path is not None:
                        remove_preview_artifacts(preview_path, output)
                    prepared = asset.model_copy(
                        update={
                            "decode_status": error.decode_status,
                            "preview_provenance": {
                                "status": "unavailable",
                                "reason": error.reason,
                                "content_sha256": asset.content_sha256,
                                "cache_key": cache_key,
                            },
                        }
                    )

            store.record(prepared, cache_key)
            processed[asset.asset_id] = prepared
            store.set_status("partial", len(inventory.assets), len(processed))

        final_assets = _current_assets(inventory.assets, processed)
        _write_assets(output, final_assets)
        build_contact_sheets(final_assets, output)
        store.set_status("completed", len(final_assets), len(final_assets))
        return Manifest(assets=final_assets)
    except BaseException as error:
        if inventory is None:
            store.set_status("failed", 0, 0, f"{type(error).__name__}: {error}")
        else:
            current = _current_assets(inventory.assets, processed)
            _write_assets(output, current)
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
