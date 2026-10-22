"""Preview rendering and contact-sheet generation."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

from silverloupe.contracts import Asset, RunConfig


RAW_EXTENSIONS = {
    ".3fr",
    ".arw",
    ".cr2",
    ".cr3",
    ".dng",
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


class PreviewUnavailable(Exception):
    """A source cannot currently produce a trustworthy preview."""

    def __init__(self, decode_status: str, reason: str) -> None:
        super().__init__(reason)
        self.decode_status = decode_status
        self.reason = reason


def _source_path(asset: Asset, config: RunConfig) -> Path:
    for root_value in config.source_roots:
        root = Path(root_value).expanduser().resolve()
        candidate = (root / asset.relative_path).resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            return candidate
    raise FileNotFoundError(f"source file for asset {asset.asset_id!r} was not found")


def _raw_image(path: Path) -> Image.Image:
    try:
        import rawpy
    except ImportError as error:
        raise PreviewUnavailable("unsupported", "raw_backend_unavailable") from error

    try:
        with rawpy.imread(str(path)) as raw:
            try:
                thumb = raw.extract_thumb()
            except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
                return Image.fromarray(
                    raw.postprocess(use_camera_wb=True, half_size=True, output_bps=8)
                )
            if thumb.format == rawpy.ThumbFormat.JPEG:
                with Image.open(BytesIO(thumb.data)) as image:
                    image.load()
                    return image.copy()
            if thumb.format == rawpy.ThumbFormat.BITMAP:
                return Image.fromarray(thumb.data)
            raise PreviewUnavailable("unsupported", "raw_thumbnail_format_unsupported")
    except PreviewUnavailable:
        raise
    except Exception as error:
        raise PreviewUnavailable("unsupported", f"raw_decode_unavailable:{type(error).__name__}") from error


def _raster_image(path: Path) -> tuple[Image.Image, bytes | None]:
    try:
        with Image.open(path) as opened:
            icc_profile = opened.info.get("icc_profile")
            opened.load()
            try:
                normalized = ImageOps.exif_transpose(opened)
            except (KeyError, TypeError, ValueError):
                normalized = opened.copy()
            return normalized.copy(), icc_profile
    except (OSError, UnidentifiedImageError) as error:
        raise PreviewUnavailable("error", f"decode_error:{type(error).__name__}") from error


def render_preview(asset: Asset, config: RunConfig) -> Path:
    """Render or reuse a normalized JPEG overview and return its absolute path."""

    source = _source_path(asset, config)
    output = Path(config.output_root).expanduser().resolve()
    preview_dir = output / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_path = preview_dir / f"{asset.asset_id}-{asset.content_sha256[:12]}.jpg"
    if preview_path.is_file():
        return preview_path

    if source.suffix.lower() in RAW_EXTENSIONS:
        image = _raw_image(source)
        icc_profile = None
    else:
        image, icc_profile = _raster_image(source)

    try:
        if image.mode != "RGB":
            image = image.convert("RGB")
        max_edge = int(config.rendering_policy.get("max_edge", 1600))
        if max_edge < 1:
            raise ValueError("rendering_policy.max_edge must be positive")
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        temporary = preview_path.with_suffix(".tmp.jpg")
        save_options: dict[str, Any] = {"quality": 90, "optimize": True}
        if icc_profile:
            save_options["icc_profile"] = icc_profile
        image.save(temporary, "JPEG", **save_options)
        temporary.replace(preview_path)
        return preview_path
    finally:
        image.close()


def build_contact_sheets(assets: list[Asset], output: Path) -> list[Path]:
    """Build deterministic contact sheets with visible asset identifiers."""

    sheet_dir = output / "contact-sheets"
    sheet_dir.mkdir(parents=True, exist_ok=True)
    generated: list[Path] = []
    cell_width, cell_height = 320, 240
    columns, page_size = 4, 12

    for page_number, offset in enumerate(range(0, len(assets), page_size), start=1):
        page_assets = assets[offset : offset + page_size]
        rows = max(1, (len(page_assets) + columns - 1) // columns)
        sheet = Image.new("RGB", (columns * cell_width, rows * cell_height), "#1b1b1b")
        draw = ImageDraw.Draw(sheet)
        for index, asset in enumerate(page_assets):
            column, row = index % columns, index // columns
            x, y = column * cell_width, row * cell_height
            provenance = asset.preview_provenance or {}
            preview_relative = provenance.get("path")
            preview_path = output / preview_relative if preview_relative else None
            if preview_path and preview_path.is_file():
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
        sheet.save(path, "JPEG", quality=88, optimize=True)
        sheet.close()
        generated.append(path)

    generated_set = set(generated)
    for stale in sheet_dir.glob("contact-*.jpg"):
        if stale not in generated_set:
            stale.unlink()
    return generated
