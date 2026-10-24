from __future__ import annotations

from pathlib import Path

from silverloupe.contracts import (
    Asset,
    Decision,
    DecisionAuthority,
    DecisionStatus,
    Manifest,
    SelectionItem,
    SelectionTier,
)
from silverloupe.delivery import plan_delivery, reject_batch_hash


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
        source_root=tmp_path / "source",
        output_root=tmp_path / "output",
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
        source_root=tmp_path / "source",
        output_root=tmp_path / "output",
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
        source_root=tmp_path / "source",
        output_root=tmp_path / "output",
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
        source_root=tmp_path / "source",
        output_root=tmp_path / "output",
    )
    wrong_hash = plan_delivery(
        manifest,
        items,
        decisions,
        "xmp-sidecar",
        source_root=tmp_path / "source",
        output_root=tmp_path / "output",
        accepted_reject_hash="0" * 64,
    )
    accepted = plan_delivery(
        manifest,
        items,
        decisions,
        "xmp-sidecar",
        source_root=tmp_path / "source",
        output_root=tmp_path / "output",
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
        source_root=tmp_path / "source",
        output_root=tmp_path / "output",
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
        source_root=tmp_path / "source",
        output_root=tmp_path / "output",
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
        source_root=tmp_path / "source",
        output_root=tmp_path / "output",
    )
    native_plan = plan_delivery(
        manifest,
        [item],
        [decision],
        "lightroom-classic-native",
        source_root=tmp_path / "source",
        output_root=tmp_path / "output",
    )

    assert jpeg_plan.status == "report_only"
    assert jpeg_plan.conflicts[0]["reason"] == "format_not_verified_for_sidecar"
    assert native_plan.status == "report_only"
    assert native_plan.backend == "native"
    assert native_plan.conflicts[0]["reason"] == "native_capability_unavailable"
