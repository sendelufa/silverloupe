from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

import silverloupe.inventory as inventory_module
from silverloupe.cli import main
from silverloupe.contracts import Brief, RunConfig
from silverloupe.inventory import prepare
from silverloupe.previews import render_preview


def _image(
    path: Path,
    *,
    size: tuple[int, int] = (12, 8),
    color: tuple[int, int, int] = (20, 40, 60),
    orientation: int | None = None,
    icc_profile: bytes | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exif = Image.Exif()
    if orientation is not None:
        exif[274] = orientation
    Image.new("RGB", size, color).save(path, exif=exif, icc_profile=icc_profile)


def _config(source: Path, output: Path, *, max_edge: int = 1600) -> RunConfig:
    return RunConfig(
        source_roots=[str(source)],
        output_root=str(output),
        brief=Brief(),
        rendering_policy={"max_edge": max_edge},
    )


def _hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _assets(output: Path) -> list[dict]:
    return [json.loads(line) for line in (output / "assets.jsonl").read_text().splitlines()]


def test_prepare_preserves_sources_and_writes_manifest_previews_and_contact_sheet(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    _image(source / "normal.jpg")
    _image(
        source / "oriented.jpg",
        size=(2, 3),
        color=(100, 20, 10),
        orientation=6,
        icc_profile=b"silverloupe-test-profile",
    )
    before = _hashes(source)

    manifest = prepare(source, output, _config(source, output))

    assert _hashes(source) == before
    assert len(manifest.assets) == 2
    assert (output / "assets.jsonl").is_file()
    assert (output / "session.json").is_file()
    assert (output / "state.sqlite3").is_file()
    assert (output / "contact-sheets/contact-001.jpg").is_file()
    oriented = next(asset for asset in manifest.assets if asset.relative_path == "oriented.jpg")
    preview_path = output / oriented.preview_provenance["path"]
    with Image.open(preview_path) as preview:
        assert preview.size == (3, 2)
        assert preview.info["icc_profile"] == b"silverloupe-test-profile"
    assert oriented.exif["Orientation"] == 6
    assert oriented.preview_provenance["orientation_normalized"] is True


def test_preview_limits_long_edge(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    _image(source / "large.jpg", size=(80, 40))
    config = _config(source, output, max_edge=32)
    manifest = inventory_module.build_inventory(source, output)

    preview_path = render_preview(manifest.assets[0], config)

    with Image.open(preview_path) as preview:
        assert preview.size == (32, 16)


def test_prepare_marks_raw_without_usable_preview_as_unsupported_not_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    (source / "without-thumbnail.cr2").write_bytes(b"not-a-real-raw")

    manifest = prepare(source, output, _config(source, output))

    asset = manifest.assets[0]
    assert asset.decode_status == "unsupported"
    assert asset.preview_provenance["status"] == "unavailable"
    assert "reject" not in json.dumps(asset.model_dump(mode="json")).lower()


def test_prepare_excludes_nested_output_from_scan(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = source / "silverloupe-output"
    _image(source / "input.jpg")
    _image(output / "old-preview.jpg", color=(200, 0, 0))

    manifest = prepare(source, output, _config(source, output))

    assert [asset.relative_path for asset in manifest.assets] == ["input.jpg"]


def test_resume_reuses_completed_checkpoint_after_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    _image(source / "a.jpg")
    _image(source / "b.jpg", color=(80, 90, 100))
    config = _config(source, output)
    real_render = inventory_module.render_preview
    calls = 0

    def interrupt_second(asset, run_config):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt("simulated interruption")
        return real_render(asset, run_config)

    monkeypatch.setattr(inventory_module, "render_preview", interrupt_second)
    with pytest.raises(KeyboardInterrupt, match="simulated interruption"):
        prepare(source, output, config)
    first_preview = next((output / "previews").glob("*.jpg"))
    first_preview_bytes = first_preview.read_bytes()

    def fail_if_first_is_rendered_again(asset, run_config):
        if asset.relative_path == "a.jpg":
            raise AssertionError("completed preview was not restored from checkpoint")
        return real_render(asset, run_config)

    monkeypatch.setattr(inventory_module, "render_preview", fail_if_first_is_rendered_again)
    manifest = prepare(source, output, config)

    assert len(manifest.assets) == 2
    assert first_preview.read_bytes() == first_preview_bytes


def test_source_change_invalidates_cached_preview(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    path = source / "changing.jpg"
    _image(path, color=(10, 20, 30))
    config = _config(source, output)
    first = prepare(source, output, config).assets[0]
    first_preview = first.preview_provenance["path"]

    _image(path, color=(200, 210, 220))
    second = prepare(source, output, config).assets[0]

    assert second.content_sha256 != first.content_sha256
    assert second.preview_provenance["path"] != first_preview
    assert (output / second.preview_provenance["path"]).is_file()


def test_cli_prepare_status_and_resume_work_from_another_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    elsewhere = tmp_path / "elsewhere"
    _image(source / "frame.jpg")
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert main(["prepare", str(source), str(output)]) == 0
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["status"] == "completed"
    assert prepared["assets"] == 1

    assert main(["status", str(output)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "completed"
    assert status["completed"] == 1

    preview = next((output / "previews").glob("*.jpg"))
    preview.unlink()
    assert main(["resume", str(output)]) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["status"] == "completed"
    assert preview.is_file()


def test_assets_jsonl_matches_returned_manifest(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    _image(source / "one.jpg")

    manifest = prepare(source, output, _config(source, output))

    assert _assets(output) == [asset.model_dump(mode="json") for asset in manifest.assets]
