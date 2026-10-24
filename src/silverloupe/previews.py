"""Preview rendering and contact-sheet generation."""

from __future__ import annotations

import hashlib
import json
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any

import PIL
from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

from silverloupe.contracts import Asset, RunConfig


RAW_EXTENSIONS = {
    ".3fr", ".arw", ".cr2", ".cr3", ".dng", ".erf", ".fff", ".iiq", ".kdc",
    ".mef", ".mos", ".mrw", ".nef", ".nrw", ".orf", ".pef", ".raf", ".raw",
    ".rw2", ".rwl", ".sr2", ".srf", ".srw", ".x3f",
}

_RENDERER_VERSION = 2
_OWNED_DIRECTORIES = ("previews", "contact-sheets", "report", "delivery")
_OWNED_FILES = (
    "assets.jsonl",
    "session.json",
    "state.sqlite3",
    "groups.json",
    "selection-items.json",
    "grouping-report.json",
    "assessments.jsonl",
    "decisions.jsonl",
    "assessment-report.json",
    "assessment-generation.json",
    "vision-request.json",
    "vision-status.json",
    "selection.json",
    "curation-report.json",
    "curation-generation.json",
    "run-state.json",
    "manual-overrides.jsonl",
    "override-generation.json",
    "report-generation.json",
    "delivery-plan.json",
    "delivery-receipt.json",
    "delivery-rollback-receipt.json",
)


class PreviewUnavailable(Exception):
    """A source cannot currently produce a trustworthy preview."""

    def __init__(self, decode_status: str, reason: str) -> None:
        super().__init__(reason)
        self.decode_status = decode_status
        self.reason = reason


def _is_inside(path: Path, directory: Path) -> bool:
    return path == directory or path.is_relative_to(directory)


def validate_output_layout(output: Path) -> None:
    """Reject links or incompatible entries at every Silverloupe-owned path."""

    root = output.expanduser().resolve()
    for name in (*_OWNED_DIRECTORIES, *_OWNED_FILES):
        candidate = root / name
        if candidate.is_symlink():
            raise ValueError(f"unsafe output path is a symlink: {candidate}")
    for name in _OWNED_DIRECTORIES:
        candidate = root / name
        if candidate.exists() and not candidate.is_dir():
            raise ValueError(f"unsafe output path is not a directory: {candidate}")
    for name in _OWNED_FILES:
        candidate = root / name
        if candidate.exists() and not candidate.is_file():
            raise ValueError(f"unsafe output path is not a file: {candidate}")


def _owned_directory(output: Path, name: str) -> Path:
    root = output.expanduser().resolve()
    validate_output_layout(root)
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or not _is_inside(directory.resolve(), root):
        raise ValueError(f"unsafe output path escapes output root: {directory}")
    return directory


def _safe_relative_path(output: Path, relative: str) -> Path | None:
    value = Path(relative)
    if value.is_absolute() or ".." in value.parts:
        return None
    candidate = output / value
    if candidate.is_symlink() or not _is_inside(candidate.resolve(), output.resolve()):
        return None
    return candidate


def _source_path(asset: Asset, config: RunConfig) -> Path:
    for root_value in config.source_roots:
        root = Path(root_value).expanduser().resolve()
        candidate = (root / asset.relative_path).resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            return candidate
    raise FileNotFoundError(f"source file for asset {asset.asset_id!r} was not found")


def _raw_backend_identity() -> str:
    try:
        import rawpy
    except ImportError:
        return "unavailable"
    return f"available:{getattr(rawpy, '__version__', 'unknown')}"


def renderer_cache_key(asset: Asset, config: RunConfig) -> str:
    """Return the complete identity of inputs that affect preview output."""

    extension = Path(asset.relative_path).suffix.lower()
    backend = _raw_backend_identity() if extension in RAW_EXTENSIONS else f"pillow:{PIL.__version__}"
    payload = {
        "renderer_version": _RENDERER_VERSION,
        "backend": backend,
        "rendering_policy": config.rendering_policy,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _normalized_opened_image(opened: Image.Image) -> tuple[Image.Image, bytes | None, dict[str, Any]]:
    icc_profile = opened.info.get("icc_profile")
    try:
        orientation = opened.getexif().get(274)
    except (OSError, TypeError, ValueError):
        orientation = None
    opened.load()
    try:
        normalized = ImageOps.exif_transpose(opened)
        orientation_normalized = orientation in {2, 3, 4, 5, 6, 7, 8}
    except (KeyError, OSError, TypeError, ValueError):
        normalized = opened.copy()
        orientation_normalized = False
    return normalized.copy(), icc_profile, {
        "original_orientation": orientation,
        "orientation_normalized": orientation_normalized,
    }


def _raw_image(path: Path) -> tuple[Image.Image, bytes | None, dict[str, Any]]:
    try:
        import rawpy
    except ImportError as error:
        raise PreviewUnavailable("unsupported", "raw_backend_unavailable") from error

    try:
        with rawpy.imread(str(path)) as raw:
            try:
                thumb = raw.extract_thumb()
            except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
                image = Image.fromarray(raw.postprocess(use_camera_wb=True, half_size=True, output_bps=8))
                return image, None, {
                    "render_source": "postprocess",
                    "original_orientation": None,
                    "orientation_normalized": False,
                }
            if thumb.format == rawpy.ThumbFormat.JPEG:
                with Image.open(BytesIO(thumb.data)) as opened:
                    image, icc_profile, details = _normalized_opened_image(opened)
                details["render_source"] = "embedded-jpeg-thumbnail"
                return image, icc_profile, details
            if thumb.format == rawpy.ThumbFormat.BITMAP:
                return Image.fromarray(thumb.data), None, {
                    "render_source": "embedded-bitmap-thumbnail",
                    "original_orientation": None,
                    "orientation_normalized": False,
                }
            raise PreviewUnavailable("unsupported", "raw_thumbnail_format_unsupported")
    except PreviewUnavailable:
        raise
    except Exception as error:
        raise PreviewUnavailable("unsupported", f"raw_decode_unavailable:{type(error).__name__}") from error


def _raster_image(path: Path) -> tuple[Image.Image, bytes | None, dict[str, Any]]:
    try:
        with Image.open(path) as opened:
            image, icc_profile, details = _normalized_opened_image(opened)
        details["render_source"] = "raster"
        return image, icc_profile, details
    except (Image.DecompressionBombError, OSError, UnidentifiedImageError) as error:
        raise PreviewUnavailable("error", f"decode_error:{type(error).__name__}") from error


def preview_is_valid(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except (Image.DecompressionBombError, OSError, UnidentifiedImageError):
        return False


def _metadata_path(preview_path: Path) -> Path:
    return preview_path.with_suffix(".jpg.json")


def preview_render_details(preview_path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_metadata_path(preview_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def remove_preview_artifacts(preview_path: Path, output: Path) -> None:
    root = output.resolve()
    for candidate in (preview_path, _metadata_path(preview_path)):
        if candidate.is_symlink() or not _is_inside(candidate.resolve(), root):
            continue
        if candidate.is_file():
            candidate.unlink()


def render_preview(asset: Asset, config: RunConfig) -> Path:
    """Render or reuse a normalized JPEG overview and return its absolute path."""

    source = _source_path(asset, config)
    output = Path(config.output_root).expanduser().resolve()
    preview_dir = _owned_directory(output, "previews")
    cache_key = renderer_cache_key(asset, config)
    asset_token = hashlib.sha256(asset.asset_id.encode("utf-8")).hexdigest()[:24]
    preview_path = preview_dir / f"{asset_token}-{asset.content_sha256[:12]}-{cache_key[:12]}.jpg"
    metadata_path = _metadata_path(preview_path)
    for candidate in (preview_path, metadata_path):
        if candidate.is_symlink() or not _is_inside(candidate.resolve(), output):
            raise ValueError(f"unsafe output path: {candidate}")
    if preview_is_valid(preview_path) and metadata_path.is_file():
        return preview_path
    remove_preview_artifacts(preview_path, output)

    if source.suffix.lower() in RAW_EXTENSIONS:
        image, icc_profile, details = _raw_image(source)
        renderer = "rawpy"
    else:
        image, icc_profile, details = _raster_image(source)
        renderer = "pillow"

    temporary: Path | None = None
    temporary_metadata: Path | None = None
    try:
        if image.mode != "RGB":
            image = image.convert("RGB")
        max_edge = int(config.rendering_policy.get("max_edge", 1600))
        if max_edge < 1:
            raise ValueError("rendering_policy.max_edge must be positive")
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        save_options: dict[str, Any] = {"quality": 90, "optimize": True}
        if icc_profile:
            save_options["icc_profile"] = icc_profile
        with tempfile.NamedTemporaryFile(dir=preview_dir, suffix=".jpg", delete=False) as handle:
            temporary = Path(handle.name)
            image.save(handle, "JPEG", **save_options)
        details.update({
            "renderer": renderer,
            "cache_key": cache_key,
            "max_edge": max_edge,
            "dimensions": list(image.size),
        })
        with tempfile.NamedTemporaryFile(
            dir=preview_dir, suffix=".json", mode="w", encoding="utf-8", delete=False
        ) as handle:
            temporary_metadata = Path(handle.name)
            handle.write(json.dumps(details, ensure_ascii=False, sort_keys=True) + "\n")
        temporary.replace(preview_path)
        temporary_metadata.replace(metadata_path)
        return preview_path
    finally:
        image.close()
        if temporary is not None and temporary.is_file():
            temporary.unlink()
        if temporary_metadata is not None and temporary_metadata.is_file():
            temporary_metadata.unlink()


def build_contact_sheets(assets: list[Asset], output: Path) -> list[Path]:
    """Build deterministic contact sheets with visible asset identifiers."""

    output = output.expanduser().resolve()
    sheet_dir = _owned_directory(output, "contact-sheets")
    generated: list[Path] = []
    cell_width, cell_height = 320, 240
    columns, page_size = 4, 12

    for page_number, offset in enumerate(range(0, len(assets), page_size), start=1):
        page_assets = assets[offset : offset + page_size]
        rows = max(1, (len(page_assets) + columns - 1) // columns)
        sheet = Image.new("RGB", (columns * cell_width, rows * cell_height), "#1b1b1b")
        draw = ImageDraw.Draw(sheet)
        try:
            for index, asset in enumerate(page_assets):
                column, row = index % columns, index // columns
                x, y = column * cell_width, row * cell_height
                provenance = asset.preview_provenance or {}
                preview_relative = provenance.get("path")
                preview_path = _safe_relative_path(output, str(preview_relative)) if preview_relative else None
                if preview_path and preview_is_valid(preview_path):
                    with Image.open(preview_path) as preview:
                        preview.thumbnail((300, 190), Image.Resampling.LANCZOS)
                        left = x + (cell_width - preview.width) // 2
                        top = y + 8 + (190 - preview.height) // 2
                        sheet.paste(preview.convert("RGB"), (left, top))
                else:
                    draw.rectangle((x + 10, y + 10, x + 310, y + 198), outline="#777777", width=2)
                    draw.text((x + 20, y + 88), "preview unavailable", fill="#cccccc")
                draw.text((x + 10, y + 204), asset.asset_id, fill="white")
                draw.text((x + 10, y + 220), asset.relative_path[-46:], fill="#bbbbbb")
            path = sheet_dir / f"contact-{page_number:03d}.jpg"
            if path.is_symlink():
                raise ValueError(f"unsafe output path is a symlink: {path}")
            temporary_sheet: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(dir=sheet_dir, suffix=".jpg", delete=False) as handle:
                    temporary_sheet = Path(handle.name)
                    sheet.save(handle, "JPEG", quality=88, optimize=True)
                temporary_sheet.replace(path)
            finally:
                if temporary_sheet is not None and temporary_sheet.is_file():
                    temporary_sheet.unlink()
            generated.append(path)
        finally:
            sheet.close()

    generated_set = set(generated)
    for stale in sheet_dir.glob("contact-*.jpg"):
        if stale.is_symlink():
            raise ValueError(f"unsafe output path is a symlink: {stale}")
        if stale not in generated_set and stale.is_file():
            stale.unlink()
    return generated
