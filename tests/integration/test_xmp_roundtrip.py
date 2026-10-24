from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from silverloupe.contracts import (
    Asset,
    Decision,
    DecisionAuthority,
    DecisionStatus,
    Manifest,
    ProposedChange,
    SelectionItem,
    SelectionTier,
)
from silverloupe.delivery import plan_delivery, reject_batch_hash
from silverloupe.delivery.xmp import (
    commit_staged_xmp,
    merge_xmp,
    read_xmp,
    stage_xmp,
)


def _asset(asset_id: str, relative_path: str, *, metadata: dict | None = None) -> Asset:
    return Asset(
        asset_id=asset_id,
        relative_path=relative_path,
        content_sha256=(asset_id[0] * 64),
        media_type="image/x-raw",
        existing_metadata=metadata or {},
    )


def _capture(item_id: str, asset_id: str, *companions: str) -> SelectionItem:
    members = [asset_id, *companions]
    return SelectionItem(
        item_id=item_id,
        kind="capture",
        member_asset_ids=members,
        primary_asset_id=asset_id,
    )


def _decision(
    item_id: str,
    *,
    status: DecisionStatus = DecisionStatus.KEEP,
    tier: SelectionTier = SelectionTier.NONE,
    authority: DecisionAuthority = DecisionAuthority.PROPOSAL,
) -> Decision:
    return Decision(
        item_id=item_id,
        status=status,
        tier=tier,
        decision_authority=authority,
    )


def _changes(plan, asset_id: str) -> dict[str, object]:
    return {
        change.field: change.value
        for change in plan.proposed_changes
        if change.asset_id == asset_id
    }


def test_plan_maps_tiers_to_ratings_and_capture_uses_only_primary_asset(
    tmp_path: Path,
) -> None:
    assets = [
        _asset("a", "top.cr2"),
        _asset("b", "top.jpg"),
        _asset("c", "select.nef"),
        _asset("d", "reserve.arw"),
    ]
    items = [
        _capture("top", "a", "b"),
        _capture("select", "c"),
        _capture("reserve", "d"),
    ]
    decisions = [
        _decision("top", tier=SelectionTier.TOP),
        _decision("select", tier=SelectionTier.SELECT),
        _decision("reserve", tier=SelectionTier.RESERVE),
    ]

    plan = plan_delivery(
        Manifest(assets=assets),
        items,
        decisions,
        "xmp-sidecar",
    )

    assert plan.status == "ready"
    assert _changes(plan, "a") == {"rating": 5, "keywords": ["silverloupe:top"]}
    assert _changes(plan, "c") == {"rating": 4, "keywords": ["silverloupe:select"]}
    assert _changes(plan, "d") == {"rating": 3, "keywords": ["silverloupe:reserve"]}
    assert not _changes(plan, "b")


def test_plan_expands_composite_to_every_required_asset(tmp_path: Path) -> None:
    manifest = Manifest(
        assets=[_asset("a", "pano-1.cr2"), _asset("b", "pano-2.cr2"), _asset("c", "preview.jpg")]
    )
    item = SelectionItem(
        item_id="pano",
        kind="composite",
        member_asset_ids=["a", "b", "c"],
        primary_asset_id="a",
        required_asset_ids=["a", "b"],
    )

    plan = plan_delivery(
        manifest,
        [item],
        [_decision("pano", tier=SelectionTier.TOP)],
        "xmp-sidecar",
    )

    assert {mapping.asset_id for mapping in plan.asset_mappings} == {"a", "b"}
    assert _changes(plan, "a")["rating"] == 5
    assert _changes(plan, "b")["rating"] == 5
    assert not _changes(plan, "c")


def test_existing_user_rating_is_a_conflict_without_prior_ownership(tmp_path: Path) -> None:
    manifest = Manifest(assets=[_asset("a", "rated.cr2", metadata={"rating": 2})])

    plan = plan_delivery(
        manifest,
        [_capture("rated", "a")],
        [_decision("rated", tier=SelectionTier.RESERVE)],
        "xmp-sidecar",
    )

    assert "rating" not in _changes(plan, "a")
    assert any(conflict["reason"] == "user_value_not_owned" for conflict in plan.conflicts)


def test_proposed_reject_needs_exact_batch_hash_before_reject_value(tmp_path: Path) -> None:
    manifest = Manifest(assets=[_asset("a", "reject.cr2")])
    items = [_capture("reject", "a")]
    decisions = [_decision("reject", status=DecisionStatus.REJECT)]

    proposed = plan_delivery(
        manifest,
        items,
        decisions,
        "xmp-sidecar",
    )
    wrong_hash = plan_delivery(
        manifest,
        items,
        decisions,
        "xmp-sidecar",
        accepted_reject_hash="0" * 64,
    )
    accepted = plan_delivery(
        manifest,
        items,
        decisions,
        "xmp-sidecar",
        accepted_reject_hash=reject_batch_hash(decisions),
    )

    assert _changes(proposed, "a") == {"keywords": ["silverloupe:suggested-reject"]}
    assert _changes(wrong_hash, "a") == {"keywords": ["silverloupe:suggested-reject"]}
    assert any(conflict["reason"] == "reject_acceptance_hash_mismatch" for conflict in wrong_hash.conflicts)
    assert _changes(accepted, "a") == {"rating": -1, "keywords": ["silverloupe:reject"]}


def test_user_authority_reject_does_not_need_batch_hash(tmp_path: Path) -> None:
    manifest = Manifest(assets=[_asset("a", "reject.nef")])

    plan = plan_delivery(
        manifest,
        [_capture("reject", "a")],
        [
            _decision(
                "reject",
                status=DecisionStatus.REJECT,
                authority=DecisionAuthority.USER,
            )
        ],
        "xmp-sidecar",
    )

    assert _changes(plan, "a")["rating"] == -1


def test_same_stem_sidecar_collision_is_reported_and_not_planned(tmp_path: Path) -> None:
    manifest = Manifest(assets=[_asset("a", "same.cr2"), _asset("b", "same.nef")])

    plan = plan_delivery(
        manifest,
        [_capture("a-item", "a"), _capture("b-item", "b")],
        [
            _decision("a-item", tier=SelectionTier.TOP),
            _decision("b-item", tier=SelectionTier.SELECT),
        ],
        "xmp-sidecar",
    )

    assert plan.status == "report_only"
    assert not plan.asset_mappings
    assert {conflict["asset_id"] for conflict in plan.conflicts} == {"a", "b"}
    assert {conflict["reason"] for conflict in plan.conflicts} == {"sidecar_path_collision"}


def test_unverified_or_embedded_metadata_targets_remain_report_only(tmp_path: Path) -> None:
    jpeg = Asset(
        asset_id="j",
        relative_path="frame.jpg",
        content_sha256="f" * 64,
        media_type="image/jpeg",
    )
    manifest = Manifest(assets=[jpeg])
    item = _capture("frame", "j")
    decision = _decision("frame", tier=SelectionTier.TOP)

    jpeg_plan = plan_delivery(
        manifest,
        [item],
        [decision],
        "xmp-sidecar",
    )
    native_plan = plan_delivery(
        manifest,
        [item],
        [decision],
        "lightroom-classic-native",
    )

    assert jpeg_plan.status == "report_only"
    assert jpeg_plan.conflicts[0]["reason"] == "format_not_verified_for_sidecar"
    assert native_plan.status == "report_only"
    assert native_plan.backend == "native"
    assert native_plan.conflicts[0]["reason"] == "native_capability_unavailable"


def test_merge_xmp_creates_minimal_packet_with_unicode_keywords() -> None:
    changes = [
        # Different item IDs are irrelevant once changes are grouped by asset.
        # The merge contract is deliberately field-level.
        ProposedChange(asset_id="a", item_id="i", field="rating", value=5),
        ProposedChange(
            asset_id="a",
            item_id="i",
            field="keywords",
            value=["silverloupe:top", "Клиент:избранное"],
        ),
    ]

    encoded, values = merge_xmp(None, changes, {})

    assert values == {
        "rating": 5,
        "label": None,
        "keywords": ["silverloupe:top", "Клиент:избранное"],
    }
    assert b"xmpmeta" in encoded
    assert "Клиент:избранное" in encoded.decode("utf-8")


def test_merge_xmp_preserves_unknown_and_develop_fields_and_replaces_only_owned_values(
    tmp_path: Path,
) -> None:
    original = b"""<?xml version="1.0" encoding="UTF-8"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description xmlns:xmp="http://ns.adobe.com/xap/1.0/"
      xmlns:dc="http://purl.org/dc/elements/1.1/"
      xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
      xmlns:custom="urn:example:custom"
      xmp:Rating="2" crs:Exposure2012="+0.25" custom:Token="keep-me">
      <xmp:Rating>2</xmp:Rating>
      <dc:subject><rdf:Bag>
        <rdf:li>user-keyword</rdf:li>
        <rdf:li>silverloupe:top</rdf:li>
      </rdf:Bag></dc:subject>
      <custom:Nested><rdf:Description custom:Value="still-here" /></custom:Nested>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>"""
    changes = [
        ProposedChange(asset_id="a", item_id="i", field="rating", value=4),
        ProposedChange(
            asset_id="a", item_id="i", field="keywords", value=["silverloupe:select"]
        ),
    ]

    encoded, values = merge_xmp(
        original,
        changes,
        {"keywords": {"owned": ["silverloupe:top"]}, "rating": {"last": 5}},
    )
    sidecar = tmp_path / "roundtrip.xmp"
    sidecar.write_bytes(encoded)

    assert values["rating"] == 4
    assert values["keywords"] == ["user-keyword", "silverloupe:select"]
    assert read_xmp(sidecar) == values
    text = encoded.decode("utf-8")
    assert "+0.25" in text
    assert "keep-me" in text
    assert "still-here" in text
    assert text.count(">4<") == 0  # canonical writer removes duplicate element form


def test_merge_xmp_finds_owned_fields_across_multiple_descriptions(
    tmp_path: Path,
) -> None:
    original = b"""<?xml version="1.0" encoding="UTF-8"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description xmlns:custom="urn:example:custom" custom:Token="keep-me" />
    <rdf:Description xmlns:xmp="http://ns.adobe.com/xap/1.0/"
      xmlns:dc="http://purl.org/dc/elements/1.1/" xmp:Rating="2">
      <dc:subject><rdf:Bag><rdf:li>user-keyword</rdf:li></rdf:Bag></dc:subject>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>"""
    changes = [
        ProposedChange(asset_id="a", item_id="i", field="rating", value=4),
        ProposedChange(
            asset_id="a", item_id="i", field="keywords", value=["silverloupe:select"]
        ),
    ]

    encoded, values = merge_xmp(original, changes, {})
    sidecar = tmp_path / "multiple-descriptions.xmp"
    sidecar.write_bytes(encoded)

    assert values == {
        "rating": 4,
        "label": None,
        "keywords": ["user-keyword", "silverloupe:select"],
    }
    assert read_xmp(sidecar) == values
    assert b"keep-me" in encoded


def test_merge_xmp_rejects_doctype_instead_of_replacing_invalid_packet() -> None:
    packet = b'<!DOCTYPE x [<!ENTITY boom SYSTEM "file:///etc/passwd">]><x>&boom;</x>'

    with pytest.raises(ValueError, match="DOCTYPE|entity"):
        merge_xmp(
            packet,
            [ProposedChange(asset_id="a", item_id="i", field="rating", value=5)],
            {},
        )


def test_stage_hashes_preimages_backs_up_and_commits_only_adjacent_sidecar(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    output_root = tmp_path / "output"
    source_root.mkdir()
    source = source_root / "nested" / "frame.cr2"
    source.parent.mkdir()
    source.write_bytes(b"raw-fixture")
    sidecar = source.with_suffix(".xmp")
    original = merge_xmp(
        None,
        [
            ProposedChange(asset_id="a", item_id="i", field="keywords", value=["user-keyword"])
        ],
        {},
    )[0]
    sidecar.write_bytes(original)
    asset = Asset(
        asset_id="a",
        relative_path="nested/frame.cr2",
        content_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        media_type="image/x-raw",
    )
    plan = plan_delivery(
        Manifest(assets=[asset]),
        [_capture("frame", "a")],
        [_decision("frame", tier=SelectionTier.TOP)],
        "xmp-sidecar",
        source_root=source_root,
        output_root=output_root,
    )

    staged = stage_xmp(plan, plan.asset_mappings[0])

    assert sidecar.read_bytes() == original
    assert staged.staged_path.is_file()
    assert staged.backup_path is not None and staged.backup_path.read_bytes() == original
    assert staged.readback["rating"] == 5
    assert staged.readback["keywords"] == ["user-keyword", "silverloupe:top"]

    readback = commit_staged_xmp(staged)

    assert readback == staged.readback
    assert sidecar.read_bytes() == staged.staged_path.read_bytes()
