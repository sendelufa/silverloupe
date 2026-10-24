from __future__ import annotations

import hashlib
from typing import Any

from silverloupe.contracts import (
    Asset,
    Decision,
    DecisionAuthority,
    DecisionStatus,
    Manifest,
    SelectionItem,
    SelectionTier,
)
from silverloupe.delivery.lightcraft import plan_lightcraft


def _asset(asset_id: str, relative_path: str) -> Asset:
    return Asset(
        asset_id=asset_id,
        relative_path=relative_path,
        content_sha256=hashlib.sha256(asset_id.encode()).hexdigest(),
        media_type="image/jpeg",
        decode_status="ready",
    )


def _item(asset: Asset) -> SelectionItem:
    return SelectionItem(
        item_id=f"item-{asset.asset_id}",
        kind="capture",
        member_asset_ids=[asset.asset_id],
        primary_asset_id=asset.asset_id,
    )


def _decision(
    asset: Asset,
    tier: SelectionTier,
    *,
    status: DecisionStatus = DecisionStatus.KEEP,
    authority: DecisionAuthority = DecisionAuthority.PROPOSAL,
) -> Decision:
    return Decision(
        item_id=f"item-{asset.asset_id}",
        status=status,
        tier=tier,
        decision_authority=authority,
    )


class FakeTransport:
    def __init__(
        self,
        assets: list[Asset],
        *,
        commands: set[str] | None = None,
        label_mapping: dict[str, str] | None = None,
    ) -> None:
        self.commands = commands if commands is not None else {
            "photo.rate",
            "photo.flag",
            "photo.label",
            "catalog.query",
        }
        self.label_mapping = label_mapping or {}
        self.mapping = {
            asset.asset_id: {
                "asset_id": asset.asset_id,
                "photo_id": index + 101,
                "content_sha256": asset.content_sha256,
            }
            for index, asset in enumerate(assets)
        }
        self.photos = {
            index + 101: {
                "id": index + 101,
                "fileName": asset.relative_path.rsplit("/", 1)[-1],
                "rating": 0,
                "flag": "none",
                "label": None,
            }
            for index, asset in enumerate(assets)
        }
        self.calls: list[str] = []

    def discover(self) -> dict[str, Any]:
        self.calls.append("discover")
        return {
            "commands": sorted(self.commands),
            "label_mapping": self.label_mapping,
        }

    def resolve(self, assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.calls.append("resolve")
        return [self.mapping[asset["asset_id"]] for asset in assets]

    def read(self, ids: list[int]) -> list[dict[str, Any]]:
        self.calls.append("read")
        return [dict(self.photos[photo_id]) for photo_id in ids if photo_id in self.photos]

    def batch(self, operations: list[dict[str, Any]]) -> dict[str, Any]:
        raise AssertionError("planning must not mutate LightCraft")


def test_missing_mcp_capability_is_report_only_without_mapping() -> None:
    asset = _asset("a", "one.jpg")
    transport = FakeTransport([asset], commands={"catalog.query"})

    plan = plan_lightcraft(
        Manifest(assets=[asset]),
        [_item(asset)],
        [_decision(asset, SelectionTier.TOP)],
        transport,
    )

    assert plan.status == "report_only"
    assert plan.backend == "unavailable"
    assert {conflict["reason"] for conflict in plan.conflicts} == {
        "lightcraft_capability_missing"
    }
    assert transport.calls == ["discover"]


def test_explicit_ids_disambiguate_identical_file_names() -> None:
    first = _asset("first", "folder-a/same.jpg")
    second = _asset("second", "folder-b/same.jpg")
    transport = FakeTransport([first, second])

    plan = plan_lightcraft(
        Manifest(assets=[first, second]),
        [_item(first), _item(second)],
        [
            _decision(first, SelectionTier.TOP),
            _decision(second, SelectionTier.SELECT),
        ],
        transport,
    )

    assert plan.status == "ready"
    assert plan.backend == "native"
    assert [(mapping.asset_id, mapping.target_id) for mapping in plan.asset_mappings] == [
        ("first", "101"),
        ("second", "102"),
    ]
    assert {
        (change.asset_id, change.field, change.value)
        for change in plan.proposed_changes
    } == {
        ("first", "rating", 5),
        ("second", "rating", 4),
    }


def test_stale_mapping_hash_is_a_conflict_and_never_becomes_ready() -> None:
    asset = _asset("a", "one.jpg")
    transport = FakeTransport([asset])
    transport.mapping[asset.asset_id]["content_sha256"] = "f" * 64

    plan = plan_lightcraft(
        Manifest(assets=[asset]),
        [_item(asset)],
        [_decision(asset, SelectionTier.TOP)],
        transport,
    )

    assert plan.status == "report_only"
    assert plan.asset_mappings == []
    assert plan.conflicts[0]["reason"] == "lightcraft_mapping_stale"


def test_proposed_reject_does_not_emit_flag_but_user_reject_does() -> None:
    proposal = _asset("proposal", "proposal.jpg")
    accepted = _asset("accepted", "accepted.jpg")
    transport = FakeTransport([proposal, accepted])

    plan = plan_lightcraft(
        Manifest(assets=[proposal, accepted]),
        [_item(proposal), _item(accepted)],
        [
            _decision(
                proposal,
                SelectionTier.NONE,
                status=DecisionStatus.REJECT,
            ),
            _decision(
                accepted,
                SelectionTier.NONE,
                status=DecisionStatus.REJECT,
                authority=DecisionAuthority.USER,
            ),
        ],
        transport,
    )

    assert not any(
        change.asset_id == "proposal" and change.field == "flag"
        for change in plan.proposed_changes
    )
    assert any(
        change.asset_id == "accepted"
        and change.field == "flag"
        and change.value == "reject"
        for change in plan.proposed_changes
    )


def test_color_label_requires_explicit_mapping() -> None:
    asset = _asset("a", "one.jpg")
    without_mapping = FakeTransport([asset])
    with_mapping = FakeTransport([asset], label_mapping={"top": "red"})

    unlabelled = plan_lightcraft(
        Manifest(assets=[asset]),
        [_item(asset)],
        [_decision(asset, SelectionTier.TOP)],
        without_mapping,
    )
    labelled = plan_lightcraft(
        Manifest(assets=[asset]),
        [_item(asset)],
        [_decision(asset, SelectionTier.TOP)],
        with_mapping,
    )

    assert not any(change.field == "label" for change in unlabelled.proposed_changes)
    assert any(
        change.field == "label" and change.value == "red"
        for change in labelled.proposed_changes
    )


def test_unknown_current_flag_or_label_is_preserved_as_a_conflict() -> None:
    asset = _asset("a", "one.jpg")
    transport = FakeTransport([asset], label_mapping={"top": "red"})
    transport.photos[101].update({"flag": "future-flag", "label": "orange"})

    plan = plan_lightcraft(
        Manifest(assets=[asset]),
        [_item(asset)],
        [_decision(asset, SelectionTier.TOP)],
        transport,
    )

    assert {conflict["field"] for conflict in plan.conflicts} == {"flag", "label"}
    assert not any(change.field == "label" for change in plan.proposed_changes)
