from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from silverloupe.cli import main
from silverloupe.comparisons import make_comparison_batches
from silverloupe.contracts import Asset, Brief, Group, Manifest, RunConfig
from silverloupe.grouping import build_selection_items, candidate_pair_count, group_assets
from silverloupe.state import write_session


def _asset(
    asset_id: str,
    path: str,
    *,
    digest: str | None = None,
    captured: str | None = "2026:10:22 12:00:00",
    camera: str | None = "camera-a",
    exposure: tuple[int, int] | None = None,
    focus_distance: float | None = None,
    sequence_type: str | list[str] | None = None,
    sequence_id: str | None = None,
    preview: str | None = None,
) -> Asset:
    exif: dict[str, object] = {}
    if captured is not None:
        exif["DateTimeOriginal"] = captured
    if camera is not None:
        exif.update({"Make": "Test", "Model": "Body", "BodySerialNumber": camera})
    if exposure is not None:
        exif["ExposureTime"] = list(exposure)
    if focus_distance is not None:
        exif["FocusDistance"] = focus_distance
    existing: dict[str, object] = {}
    if sequence_type is not None:
        existing["sequence_type"] = sequence_type
    if sequence_id is not None:
        existing["sequence_id"] = sequence_id
    suffix = Path(path).suffix.lower()
    return Asset(
        asset_id=asset_id,
        relative_path=path,
        content_sha256=digest or hashlib.sha256(asset_id.encode()).hexdigest(),
        media_type="image/x-raw" if suffix in {".cr2", ".nef", ".dng"} else "image/jpeg",
        exif=exif,
        existing_metadata=existing,
        preview_provenance={"status": "ready", "path": preview} if preview else None,
    )


def _config(output: Path, **grouping_policy: object) -> RunConfig:
    return RunConfig(
        source_roots=[str(output / "source")],
        output_root=str(output),
        brief=Brief(),
        rendering_policy={},
        budget={"grouping_policy": grouping_policy},
    )


def _preview(output: Path, name: str, *, inverse: bool = False) -> str:
    relative = f"previews/{name}.jpg"
    path = output / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("L", (32, 32), 255 if inverse else 0)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 15, 31), fill=0 if inverse else 255)
    image.save(path, "JPEG")
    return relative


def _one(groups, group_type: str):
    matches = [group for group in groups if group.type == group_type]
    assert len(matches) == 1
    return matches[0]


def test_groups_identical_bytes_without_losing_distinct_assets(tmp_path: Path) -> None:
    digest = "a" * 64
    manifest = Manifest(
        assets=[
            _asset("a", "one/photo.jpg", digest=digest),
            _asset("b", "two/photo.jpg", digest=digest),
        ]
    )

    group = _one(group_assets(manifest, _config(tmp_path)), "byte_duplicate")

    assert group.member_ids == ["a", "b"]
    assert group.protected_set is False


def test_raw_jpeg_pair_requires_matching_capture_evidence(tmp_path: Path) -> None:
    valid = Manifest(
        assets=[
            _asset("raw", "shoot/IMG_0042.CR2"),
            _asset("jpeg", "shoot/IMG_0042.JPG"),
        ]
    )

    pair = _one(group_assets(valid, _config(tmp_path)), "raw_jpeg_companion")
    assert pair.member_ids == ["jpeg", "raw"]

    false_stem = Manifest(
        assets=[
            _asset("raw", "shoot/IMG_0042.CR2", camera="camera-a", captured="2026:10:22 12:00:00"),
            _asset("jpeg", "shoot/IMG_0042.JPG", camera="camera-b", captured="2026:10:23 12:00:00"),
        ]
    )
    groups = group_assets(false_stem, _config(tmp_path))

    assert not [group for group in groups if group.type in {"raw_jpeg_companion", "uncertain"}]
    assert len(build_selection_items(false_stem, groups)) == 2


def test_unique_same_directory_raw_jpeg_pair_falls_back_to_file_identity(
    tmp_path: Path,
) -> None:
    manifest = Manifest(
        assets=[
            _asset("raw", "shoot/IMG_0042.ARW", captured=None, camera=None),
            _asset(
                "jpeg",
                "shoot/IMG_0042.JPG",
                captured=None,
                camera=None,
                preview=_preview(tmp_path, "IMG_0042"),
            ),
        ]
    )

    groups = group_assets(manifest, _config(tmp_path))
    pair = _one(groups, "raw_jpeg_companion")
    items = build_selection_items(manifest, groups)

    assert pair.member_ids == ["jpeg", "raw"]
    assert pair.confidence_kind == "heuristic"
    assert pair.evidence == [
        {"kind": "unique_same_stem_companion", "stem": "img_0042"}
    ]
    assert len(items) == 1
    assert items[0].primary_asset_id == "raw"


@pytest.mark.parametrize(
    ("captured", "camera"),
    [(None, "camera-a"), ("2026:10:22 12:00:00", None)],
)
def test_unique_raw_jpeg_pair_with_incomplete_metadata_uses_name_identity(
    tmp_path: Path, captured: str | None, camera: str | None
) -> None:
    manifest = Manifest(
        assets=[
            _asset("raw", "shoot/IMG_0042.CR2", captured=captured, camera=camera),
            _asset("jpeg", "shoot/IMG_0042.JPG", captured=captured, camera=camera),
        ]
    )

    groups = group_assets(manifest, _config(tmp_path))

    pair = _one(groups, "raw_jpeg_companion")
    assert pair.member_ids == ["jpeg", "raw"]
    assert pair.confidence_value == 0.9
    assert pair.evidence == [
        {"kind": "unique_same_stem_companion", "stem": "img_0042"}
    ]
    assert len(build_selection_items(manifest, groups)) == 1


def test_same_stem_raw_jpeg_with_conflicting_timestamps_stays_separate(
    tmp_path: Path,
) -> None:
    manifest = Manifest(
        assets=[
            _asset(
                "raw",
                "shoot/IMG_0042.CR2",
                captured="2026:10:22 12:00:00",
                camera=None,
            ),
            _asset(
                "jpeg",
                "shoot/IMG_0042.JPG",
                captured="2026:10:22 12:00:10",
                camera=None,
            ),
        ]
    )

    groups = group_assets(manifest, _config(tmp_path))

    assert not [group for group in groups if group.type == "raw_jpeg_companion"]
    assert len(build_selection_items(manifest, groups)) == 2


def test_groups_visually_similar_close_frames_as_burst(tmp_path: Path) -> None:
    preview = _preview(tmp_path, "burst")
    manifest = Manifest(
        assets=[
            _asset("a", "a.jpg", captured="2026:10:22 12:00:00", preview=preview),
            _asset("b", "b.jpg", captured="2026:10:22 12:00:01", preview=preview),
            _asset("c", "c.jpg", captured="2026:10:22 12:00:02", preview=preview),
        ]
    )

    group = _one(group_assets(manifest, _config(tmp_path)), "burst")

    assert group.member_ids == ["a", "b", "c"]
    assert group.confidence_kind == "measured"


def test_same_clock_time_on_different_cameras_does_not_form_burst(tmp_path: Path) -> None:
    preview = _preview(tmp_path, "same-view")
    manifest = Manifest(
        assets=[
            _asset("a", "a.jpg", camera="camera-a", preview=preview),
            _asset("b", "b.jpg", camera="camera-b", preview=preview),
        ]
    )

    groups = group_assets(manifest, _config(tmp_path))

    assert not [group for group in groups if group.type in {"burst", "near_duplicate"}]


def test_exposure_bracket_becomes_protected_hdr(tmp_path: Path) -> None:
    preview = _preview(tmp_path, "hdr")
    exposures = [(1, 8), (1, 15), (1, 30), (1, 60), (1, 125)]
    manifest = Manifest(
        assets=[
            _asset(
                f"h{index}",
                f"h{index}.dng",
                captured=f"2026:10:22 12:00:0{index}",
                exposure=exposure,
                preview=preview,
            )
            for index, exposure in enumerate(exposures)
        ]
    )

    group = _one(group_assets(manifest, _config(tmp_path)), "hdr")

    assert group.protected_set is True
    assert len(group.member_ids) == 5


@pytest.mark.parametrize(
    ("exposures", "focus_distances"),
    [
        ([(1, 200), (1, 250), (1, 300)], [None, None, None]),
        ([None, None, None], [1.0, 1.1, 1.2]),
    ],
)
def test_weak_bracket_variation_stays_uncertain_and_uncompressed(
    tmp_path: Path,
    exposures: list[tuple[int, int] | None],
    focus_distances: list[float | None],
) -> None:
    preview = _preview(tmp_path, "weak-bracket")
    manifest = Manifest(
        assets=[
            _asset(
                f"a{index}",
                f"a{index}.jpg",
                captured=f"2026:10:22 12:00:0{index}",
                exposure=exposures[index],
                focus_distance=focus_distances[index],
                preview=preview,
            )
            for index in range(3)
        ]
    )

    groups = group_assets(manifest, _config(tmp_path))

    assert not [group for group in groups if group.protected_set]
    assert _one(groups, "uncertain").member_ids == ["a0", "a1", "a2"]
    assert len(build_selection_items(manifest, groups)) == 3


@pytest.mark.parametrize("sequence_type", ["panorama", "focus_stack"])
def test_metadata_backed_composite_is_protected(tmp_path: Path, sequence_type: str) -> None:
    preview = _preview(tmp_path, sequence_type)
    manifest = Manifest(
        assets=[
            _asset(
                f"{sequence_type}-{index}",
                f"{sequence_type}-{index}.jpg",
                captured=f"2026:10:22 12:00:0{index}",
                focus_distance=float(index + 1) if sequence_type == "focus_stack" else None,
                sequence_type=sequence_type,
                sequence_id="set-1",
                preview=preview,
            )
            for index in range(3)
        ]
    )

    group = _one(group_assets(manifest, _config(tmp_path)), sequence_type)

    assert group.protected_set is True


def test_asset_can_have_overlapping_nonexclusive_relations(tmp_path: Path) -> None:
    digest = "b" * 64
    manifest = Manifest(
        assets=[
            _asset("raw", "IMG_1.CR2", digest=digest),
            _asset("jpeg", "IMG_1.JPG", digest=digest),
        ]
    )

    groups = group_assets(manifest, _config(tmp_path))

    assert {group.type for group in groups} == {"byte_duplicate", "raw_jpeg_companion"}
    assert all("raw" in group.member_ids for group in groups)


def test_raw_jpeg_pair_builds_one_capture_item_with_raw_primary(tmp_path: Path) -> None:
    manifest = Manifest(assets=[_asset("raw", "IMG_1.NEF"), _asset("jpeg", "IMG_1.JPG")])
    groups = group_assets(manifest, _config(tmp_path))

    items = build_selection_items(manifest, groups)

    assert len(items) == 1
    assert items[0].kind == "capture"
    assert items[0].member_asset_ids == ["jpeg", "raw"]
    assert items[0].primary_asset_id == "raw"
    assert items[0].required_asset_ids == []


def test_collapsed_capture_keeps_all_overlapping_group_references(tmp_path: Path) -> None:
    digest = "c" * 64
    manifest = Manifest(
        assets=[
            _asset("raw", "IMG_1.NEF", digest=digest),
            _asset("jpeg", "IMG_1.JPG", digest=digest),
        ]
    )
    groups = group_assets(manifest, _config(tmp_path))

    item = build_selection_items(manifest, groups)[0]

    assert set(item.group_ids) == {group.group_id for group in groups}


def test_hdr_builds_one_composite_with_all_required_assets(tmp_path: Path) -> None:
    preview = _preview(tmp_path, "hdr-items")
    manifest = Manifest(
        assets=[
            _asset(
                f"h{index}",
                f"h{index}.dng",
                captured=f"2026:10:22 12:00:0{index}",
                exposure=(1, 2 ** (index + 2)),
                preview=preview,
            )
            for index in range(5)
        ]
    )
    groups = group_assets(manifest, _config(tmp_path))

    items = build_selection_items(manifest, groups)

    assert len(items) == 1
    assert items[0].kind == "composite"
    assert items[0].required_asset_ids == ["h0", "h1", "h2", "h3", "h4"]


def test_conflicting_composite_candidates_remain_uncertain_and_uncompressed(tmp_path: Path) -> None:
    preview = _preview(tmp_path, "conflict")
    manifest = Manifest(
        assets=[
            _asset(
                f"a{index}",
                f"a{index}.jpg",
                captured=f"2026:10:22 12:00:0{index}",
                sequence_type=["hdr", "panorama"],
                sequence_id="ambiguous-set",
                preview=preview,
            )
            for index in range(3)
        ]
    )

    groups = group_assets(manifest, _config(tmp_path))
    items = build_selection_items(manifest, groups)

    assert groups
    assert {group.type for group in groups} == {"uncertain"}
    assert all(group.protected_set is False for group in groups)
    assert len(items) == 3
    assert all(item.kind == "capture" for item in items)


def test_manually_supplied_overlapping_protected_groups_are_rejected(tmp_path: Path) -> None:
    manifest = Manifest(assets=[_asset("a", "a.jpg"), _asset("b", "b.jpg"), _asset("c", "c.jpg")])
    groups = [
        Group(group_id="hdr", type="hdr", member_ids=["a", "b"], protected_set=True),
        Group(group_id="pano", type="panorama", member_ids=["b", "c"], protected_set=True),
    ]

    with pytest.raises(ValueError, match="overlapping protected groups"):
        build_selection_items(manifest, groups)


def test_companion_expansion_cannot_hide_overlapping_protected_groups(tmp_path: Path) -> None:
    manifest = Manifest(
        assets=[
            _asset("r1", "r1.dng"),
            _asset("r2", "r2.dng"),
            _asset("r3", "r3.dng"),
            _asset("j3", "r3.jpg"),
            _asset("j4", "j4.jpg"),
            _asset("j5", "j5.jpg"),
        ]
    )
    groups = [
        Group(group_id="hdr", type="hdr", member_ids=["r1", "r2", "r3"], protected_set=True),
        Group(
            group_id="pano",
            type="panorama",
            member_ids=["j3", "j4", "j5"],
            protected_set=True,
        ),
        Group(
            group_id="pair",
            type="raw_jpeg_companion",
            member_ids=["r3", "j3"],
        ),
    ]

    with pytest.raises(ValueError, match="overlapping protected groups"):
        build_selection_items(manifest, groups)


def test_duplicate_group_ids_are_rejected_before_item_building(tmp_path: Path) -> None:
    manifest = Manifest(
        assets=[
            _asset("a", "a.jpg"),
            _asset("b", "b.jpg"),
            _asset("c", "c.jpg"),
            _asset("d", "d.jpg"),
        ]
    )
    groups = [
        Group(group_id="duplicate", type="panorama", member_ids=["a", "b"], protected_set=True),
        Group(group_id="duplicate", type="focus_stack", member_ids=["c", "d"], protected_set=True),
    ]

    with pytest.raises(ValueError, match="duplicate group_id"):
        build_selection_items(manifest, groups)


def test_auto_grouping_downgrades_companion_expanded_composite_conflicts(
    tmp_path: Path,
) -> None:
    assets = [
        _asset(
            asset_id,
            path,
            sequence_type=sequence_type,
            sequence_id=sequence_id,
        )
        for asset_id, path, sequence_type, sequence_id in [
            ("r1", "r1.dng", "hdr", "hdr-set"),
            ("r2", "r2.dng", "hdr", "hdr-set"),
            ("r3", "shared.dng", "hdr", "hdr-set"),
            ("j3", "shared.jpg", "panorama", "pano-set"),
            ("j4", "j4.jpg", "panorama", "pano-set"),
            ("j5", "j5.jpg", "panorama", "pano-set"),
        ]
    ]
    manifest = Manifest(assets=assets)

    groups = group_assets(manifest, _config(tmp_path))
    items = build_selection_items(manifest, groups)

    assert not [group for group in groups if group.protected_set]
    assert len([group for group in groups if group.type == "uncertain"]) == 2
    assert sorted(member for item in items for member in item.member_asset_ids) == sorted(
        asset.asset_id for asset in assets
    )


def test_close_frames_without_visual_evidence_remain_uncertain(tmp_path: Path) -> None:
    manifest = Manifest(
        assets=[
            _asset("a", "a.jpg", captured="2026:10:22 12:00:00"),
            _asset("b", "b.jpg", captured="2026:10:22 12:00:01"),
        ]
    )

    group = _one(group_assets(manifest, _config(tmp_path)), "uncertain")
    items = build_selection_items(manifest, [group])

    assert group.confidence_kind == "unknown"
    assert len(items) == 2


def test_comparison_batches_include_review_evidence(tmp_path: Path) -> None:
    preview = _preview(tmp_path, "comparison")
    manifest = Manifest(
        assets=[
            _asset("a", "a.jpg", captured="2026:10:22 12:00:00", preview=preview),
            _asset("b", "b.jpg", captured="2026:10:22 12:00:01", preview=preview),
        ]
    )
    groups = group_assets(manifest, _config(tmp_path))

    batches = make_comparison_batches(groups, manifest)

    assert batches == [
        {
            "group_id": next(group.group_id for group in groups if group.type == "burst"),
            "group_type": "burst",
            "asset_ids": ["a", "b"],
            "preview_paths": [preview, preview],
            "protected_set": False,
            "evidence": next(group.evidence for group in groups if group.type == "burst"),
        }
    ]


@pytest.mark.parametrize("count,max_pairs", [(1000, 24_000), (5000, 120_000)])
def test_candidate_search_is_bounded_for_large_shoots(
    tmp_path: Path, count: int, max_pairs: int
) -> None:
    manifest = Manifest(
        assets=[
            _asset(f"a{index}", f"a{index}.jpg", captured="2026:10:22 12:00:00")
            for index in range(count)
        ]
    )

    comparisons = candidate_pair_count(manifest, _config(tmp_path))

    assert comparisons <= max_pairs
    assert comparisons < count * (count - 1) // 2


def test_mixed_naive_and_aware_timestamps_do_not_crash_grouping(tmp_path: Path) -> None:
    manifest = Manifest(
        assets=[
            _asset("naive", "naive.jpg", captured="2026:10:22 12:00:00"),
            _asset("aware", "aware.jpg", captured="2026-10-22T12:00:01+05:00"),
        ]
    )

    groups = group_assets(manifest, _config(tmp_path))

    assert candidate_pair_count(manifest, _config(tmp_path)) == 0
    assert {tuple(group.member_ids) for group in groups} == {("naive",), ("aware",)}


def test_hashing_falls_back_to_pillow_11_pixel_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preview = _preview(tmp_path, "pillow-11")
    manifest = Manifest(
        assets=[
            _asset("a", "a.jpg", preview=preview),
            _asset("b", "b.jpg", captured="2026:10:22 12:00:01", preview=preview),
        ]
    )
    monkeypatch.delattr(Image.Image, "get_flattened_data", raising=False)

    assert _one(group_assets(manifest, _config(tmp_path)), "burst")


def test_cli_group_writes_contract_artifacts_from_another_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "output"
    elsewhere = tmp_path / "elsewhere"
    output.mkdir()
    elsewhere.mkdir()
    manifest = Manifest(assets=[_asset("a", "a.jpg", captured=None, camera=None)])
    config = _config(output)
    write_session(output, config, "test-session")
    (output / "assets.jsonl").write_text(
        "".join(json.dumps(asset.model_dump(mode="json"), sort_keys=True) + "\n" for asset in manifest.assets),
        encoding="utf-8",
    )
    monkeypatch.chdir(elsewhere)

    assert main(["group", str(output)]) == 0
    summary = json.loads(capsys.readouterr().out)

    assert summary["status"] == "completed"
    assert summary["groups"] == 1
    assert summary["selection_items"] == 1
    assert (output / "groups.json").is_file()
    assert (output / "selection-items.json").is_file()
    report = json.loads((output / "grouping-report.json").read_text(encoding="utf-8"))
    assert report["validation"] == "synthetic-or-user-review-required"


def test_group_atomic_writes_do_not_follow_predictable_temp_symlink(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    manifest = Manifest(assets=[_asset("a", "a.jpg", captured=None, camera=None)])
    write_session(output, _config(output), "test-session")
    (output / "assets.jsonl").write_text(
        json.dumps(manifest.assets[0].model_dump(mode="json")) + "\n", encoding="utf-8"
    )
    external = tmp_path / "must-not-change.txt"
    external.write_text("original", encoding="utf-8")
    (output / ".groups.json.tmp").symlink_to(external)

    assert main(["group", str(output)]) == 0
    capsys.readouterr()

    assert external.read_text(encoding="utf-8") == "original"


def test_cli_can_rebuild_items_from_manually_corrected_groups(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    manifest = Manifest(assets=[_asset("raw", "IMG_1.NEF"), _asset("jpeg", "IMG_1.JPG")])
    write_session(output, _config(output), "test-session")
    (output / "assets.jsonl").write_text(
        "".join(json.dumps(asset.model_dump(mode="json")) + "\n" for asset in manifest.assets),
        encoding="utf-8",
    )
    manual_group = Group(
        group_id="manual-pair",
        type="raw_jpeg_companion",
        member_ids=["raw", "jpeg"],
        confidence_kind="manual",
        confidence_value=1.0,
        evidence=[{"kind": "user_correction"}],
    )
    (output / "groups.json").write_text(
        json.dumps([manual_group.model_dump(mode="json")]), encoding="utf-8"
    )

    assert main(["group", str(output), "--reuse-groups"]) == 0
    capsys.readouterr()
    items = json.loads((output / "selection-items.json").read_text(encoding="utf-8"))

    assert len(items) == 1
    assert items[0]["member_asset_ids"] == ["jpeg", "raw"]
    assert items[0]["group_ids"] == ["manual-pair"]


def test_ordinary_group_rerun_preserves_manual_corrections_until_explicit_reset(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    manifest = Manifest(
        assets=[
            _asset("a", "a.jpg", captured=None, camera=None),
            _asset("b", "b.jpg", captured=None, camera=None),
        ]
    )
    write_session(output, _config(output), "test-session")
    (output / "assets.jsonl").write_text(
        "".join(json.dumps(asset.model_dump(mode="json")) + "\n" for asset in manifest.assets),
        encoding="utf-8",
    )
    manual_group = Group(
        group_id="manual-panorama",
        type="panorama",
        member_ids=["a", "b"],
        confidence_kind="manual",
        protected_set=True,
        evidence=[{"kind": "user_correction"}],
    )
    (output / "groups.json").write_text(
        json.dumps([manual_group.model_dump(mode="json")]), encoding="utf-8"
    )

    assert main(["group", str(output)]) == 0
    capsys.readouterr()
    rerun_groups = json.loads((output / "groups.json").read_text(encoding="utf-8"))
    assert [group["group_id"] for group in rerun_groups] == ["manual-panorama"]

    assert main(["group", str(output), "--reset-manual-groups"]) == 0
    capsys.readouterr()
    reset_groups = json.loads((output / "groups.json").read_text(encoding="utf-8"))
    assert "manual-panorama" not in {group["group_id"] for group in reset_groups}


def test_invalid_policy_fails_before_replacing_any_group_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    manifest = Manifest(assets=[_asset("a", "a.jpg", captured=None, camera=None)])
    write_session(output, _config(output, max_neighbors=None), "test-session")
    (output / "assets.jsonl").write_text(
        json.dumps(manifest.assets[0].model_dump(mode="json")) + "\n", encoding="utf-8"
    )
    original = {
        "groups.json": "[]\n",
        "selection-items.json": "[]\n",
        "grouping-report.json": '{"status":"previous"}\n',
    }
    for name, contents in original.items():
        (output / name).write_text(contents, encoding="utf-8")

    assert main(["group", str(output), "--reuse-groups"]) == 2
    captured = capsys.readouterr()

    assert "max_neighbors" in captured.err
    assert {name: (output / name).read_text(encoding="utf-8") for name in original} == original


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("max_neighbors", 1.5),
        ("max_time_seconds", float("inf")),
        ("visual_similarity", -0.1),
        ("burst_seconds", "soon"),
    ],
)
def test_policy_values_are_finite_and_typed(
    tmp_path: Path, name: str, value: object
) -> None:
    manifest = Manifest(assets=[_asset("a", "a.jpg")])

    with pytest.raises(ValueError, match=name):
        candidate_pair_count(manifest, _config(tmp_path, **{name: value}))
