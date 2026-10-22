from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import ExifTags, Image

import silverloupe.inventory as inventory_module
from silverloupe.cli import main
from silverloupe.contracts import Asset, Brief, RunConfig
from silverloupe.inventory import build_inventory, prepare
from silverloupe.previews import render_preview
from silverloupe.state import read_status


def _image(
    path: Path,
    *,
    size: tuple[int, int] = (24, 12),
    color: tuple[int, int, int] = (20, 40, 60),
    exif: Image.Exif | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    options = {"exif": exif} if exif is not None else {}
    Image.new("RGB", size, color).save(path, **options)


def _config(source: Path, output: Path, *, max_edge: int = 1600) -> RunConfig:
    return RunConfig(
        source_roots=[str(source)],
        output_root=str(output),
        brief=Brief(),
        rendering_policy={"max_edge": max_edge},
    )


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_prepare_rejects_owned_output_symlink_without_touching_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    original = source / "contact-001.jpg"
    stale = source / "contact-002.jpg"
    _image(original, color=(100, 0, 0))
    _image(stale, color=(0, 100, 0))
    before = {path.name: _hash(path) for path in (original, stale)}
    output.mkdir()
    (output / "contact-sheets").symlink_to(source, target_is_directory=True)

    with pytest.raises(ValueError, match="unsafe output path"):
        prepare(source, output, _config(source, output))

    assert {path.name: _hash(path) for path in (original, stale)} == before
    assert not (output / "session.json").exists()


def test_renderer_contains_untrusted_asset_id(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    photo = source / "photo.jpg"
    _image(photo)
    asset = Asset(
        asset_id="../../escaped",
        relative_path="photo.jpg",
        content_sha256=_hash(photo),
        media_type="image/jpeg",
    )

    rendered = render_preview(asset, _config(source, output))

    assert rendered.resolve().is_relative_to((output / "previews").resolve())
    assert not list(tmp_path.glob("escaped-*"))


def test_render_settings_invalidate_checkpoint_and_preview(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    _image(source / "photo.jpg", size=(80, 40))

    first = prepare(source, output, _config(source, output, max_edge=32)).assets[0]
    second = prepare(source, output, _config(source, output, max_edge=16)).assets[0]

    assert first.preview_provenance["path"] != second.preview_provenance["path"]
    with Image.open(output / second.preview_provenance["path"]) as preview:
        assert preview.size == (16, 8)


def test_raw_backend_becoming_available_retries_unsupported_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    (source / "photo.cr2").write_bytes(b"raw-placeholder")
    first = prepare(source, output, _config(source, output)).assets[0]
    assert first.decode_status == "unsupported"

    class FakeRaw:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def extract_thumb(self):
            raise FakeRawPy.LibRawNoThumbnailError()

        def postprocess(self, **_kwargs):
            return np.zeros((10, 20, 3), dtype=np.uint8)

    FakeRawPy = SimpleNamespace(
        __version__="test-backend",
        imread=lambda _path: FakeRaw(),
        LibRawNoThumbnailError=type("LibRawNoThumbnailError", (Exception,), {}),
        LibRawUnsupportedThumbnailError=type("LibRawUnsupportedThumbnailError", (Exception,), {}),
        ThumbFormat=SimpleNamespace(JPEG=1, BITMAP=2),
    )
    monkeypatch.setitem(sys.modules, "rawpy", FakeRawPy)

    second = prepare(source, output, _config(source, output)).assets[0]

    assert second.decode_status == "ready"
    assert second.preview_provenance["render_source"] == "postprocess"


def test_prepare_validates_root_relationships_before_writing(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _image(source / "photo.jpg")

    with pytest.raises(ValueError, match="differ"):
        prepare(source, source, _config(source, source))
    assert not (source / "session.json").exists()

    with pytest.raises(ValueError, match="ancestor"):
        prepare(source, tmp_path, _config(source, tmp_path))
    assert not (tmp_path / "session.json").exists()


def test_raw_jpeg_thumbnail_is_oriented_and_preserves_icc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    raw_path = source / "photo.cr2"
    raw_path.write_bytes(b"raw-placeholder")
    thumb_path = tmp_path / "thumb.jpg"
    exif = Image.Exif()
    exif[274] = 6
    nested = exif.get_ifd(ExifTags.IFD.Exif)
    nested[36867] = "2026:02:03 04:05:06"
    nested[34855] = 800
    exif[ExifTags.IFD.Exif] = nested
    Image.new("RGB", (2, 3), (80, 20, 10)).save(
        thumb_path, exif=exif, icc_profile=b"raw-thumb-profile"
    )

    class FakeRaw:
        sizes = SimpleNamespace(width=6000, height=4000)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def extract_thumb(self):
            return SimpleNamespace(format=1, data=thumb_path.read_bytes())

    fake_rawpy = SimpleNamespace(
        __version__="test-backend",
        imread=lambda _path: FakeRaw(),
        LibRawNoThumbnailError=type("LibRawNoThumbnailError", (Exception,), {}),
        LibRawUnsupportedThumbnailError=type("LibRawUnsupportedThumbnailError", (Exception,), {}),
        ThumbFormat=SimpleNamespace(JPEG=1, BITMAP=2),
    )
    monkeypatch.setitem(sys.modules, "rawpy", fake_rawpy)

    asset = prepare(source, output, _config(source, output)).assets[0]
    with Image.open(output / asset.preview_provenance["path"]) as preview:
        assert preview.size == (3, 2)
        assert preview.info["icc_profile"] == b"raw-thumb-profile"
    assert asset.preview_provenance["orientation_normalized"] is True
    assert asset.preview_provenance["render_source"] == "embedded-jpeg-thumbnail"
    assert asset.dimensions == (6000, 4000)
    assert asset.exif["DateTimeOriginal"] == "2026:02:03 04:05:06"
    assert asset.exif["ISOSpeedRatings"] == 800


def test_inventory_extracts_nested_exif_fields(tmp_path: Path) -> None:
    source = tmp_path / "source"
    exif = Image.Exif()
    nested = exif.get_ifd(ExifTags.IFD.Exif)
    nested[36867] = "2026:01:02 03:04:05"
    nested[33434] = (1, 125)
    nested[34855] = 400
    exif[ExifTags.IFD.Exif] = nested
    _image(source / "photo.jpg", exif=exif)

    asset = build_inventory(source, tmp_path / "output").assets[0]

    assert asset.exif["DateTimeOriginal"] == "2026:01:02 03:04:05"
    assert asset.exif["ExposureTime"] == [1, 125]
    assert asset.exif["ISOSpeedRatings"] == 400


def test_decoder_safety_failure_is_accounted_without_aborting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    photo = source / "photo.jpg"
    _image(photo)
    real_open = inventory_module.Image.open

    def bomb(path, *args, **kwargs):
        if Path(path) == photo:
            raise Image.DecompressionBombError("too many pixels")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(inventory_module.Image, "open", bomb)

    asset = prepare(source, output, _config(source, output)).assets[0]

    assert asset.decode_status == "error"
    assert asset.preview_provenance["reason"] == "decode_error:DecompressionBombError"
    assert read_status(output)["status"] == "completed"


def test_scan_failure_replaces_previous_completed_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    _image(source / "photo.jpg")
    prepare(source, output, _config(source, output))
    monkeypatch.setattr(
        inventory_module, "build_inventory", lambda *_args: (_ for _ in ()).throw(RuntimeError("scan failed"))
    )

    with pytest.raises(RuntimeError, match="scan failed"):
        prepare(source, output, _config(source, output))

    status = read_status(output)
    assert status["status"] == "failed"
    assert "scan failed" in status["error"]


def test_corrupt_cached_preview_is_regenerated(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    _image(source / "photo.jpg")
    first = prepare(source, output, _config(source, output)).assets[0]
    preview = output / first.preview_provenance["path"]
    preview.write_bytes(b"broken-jpeg")

    second = prepare(source, output, _config(source, output)).assets[0]

    with Image.open(output / second.preview_provenance["path"]) as image:
        image.verify()


def test_source_change_during_render_is_reported_as_unstable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    photo = source / "photo.jpg"
    _image(photo, color=(200, 0, 0))
    real_render = inventory_module.render_preview

    def change_then_render(asset, config):
        _image(photo, color=(0, 0, 200))
        return real_render(asset, config)

    monkeypatch.setattr(inventory_module, "render_preview", change_then_render)

    asset = prepare(source, output, _config(source, output)).assets[0]

    assert asset.decode_status == "degraded"
    assert asset.preview_provenance["reason"] == "source_changed_during_prepare"
    assert "path" not in asset.preview_provenance


def test_manifest_snapshot_is_not_rewritten_for_every_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    for index in range(8):
        _image(source / f"{index}.jpg", color=(index, index, index))
    real_write = inventory_module._write_assets
    calls = 0

    def count_write(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_write(*args, **kwargs)

    monkeypatch.setattr(inventory_module, "_write_assets", count_write)

    prepare(source, output, _config(source, output))

    assert calls == 1


def test_cli_maps_corrupt_sqlite_to_environment_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    (output / "state.sqlite3").write_bytes(b"not a sqlite database")

    assert main(["status", str(output)]) == 4
    assert "database" in capsys.readouterr().err.lower()
