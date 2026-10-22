from pathlib import Path

from PIL import Image

from silverloupe.inventory import build_inventory


def _image(path: Path, color: tuple[int, int, int] = (20, 40, 60)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 6), color).save(path)


def test_inventory_keeps_same_names_and_identical_bytes_as_distinct_assets(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    first = source / "one/photo.jpg"
    second = source / "two/photo.jpg"
    _image(first)
    second.parent.mkdir(parents=True)
    second.write_bytes(first.read_bytes())

    manifest = build_inventory(source, output)

    assert [asset.relative_path for asset in manifest.assets] == ["one/photo.jpg", "two/photo.jpg"]
    assert len({asset.asset_id for asset in manifest.assets}) == 2
    assert len({asset.content_sha256 for asset in manifest.assets}) == 1


def test_inventory_handles_unicode_spaces_and_stable_ids(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    _image(source / " Свадьба /кадр № 1.png")

    first = build_inventory(source, output)
    second = build_inventory(source, output)

    assert first.assets[0].relative_path == " Свадьба /кадр № 1.png"
    assert first.assets[0].asset_id == second.assets[0].asset_id


def test_inventory_excludes_output_nested_inside_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = source / "silverloupe-output"
    _image(source / "keep.jpg")
    _image(output / "must-not-be-scanned.jpg", color=(200, 10, 10))

    manifest = build_inventory(source, output)

    assert [asset.relative_path for asset in manifest.assets] == ["keep.jpg"]


def test_inventory_tolerates_invalid_exif_orientation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    path = source / "invalid-orientation.jpg"
    path.parent.mkdir(parents=True)
    exif = Image.Exif()
    exif[274] = 99
    Image.new("RGB", (8, 6), (50, 100, 150)).save(path, exif=exif)

    manifest = build_inventory(source, output)

    assert manifest.assets[0].decode_status == "ready"
    assert manifest.assets[0].exif["Orientation"] == 99
