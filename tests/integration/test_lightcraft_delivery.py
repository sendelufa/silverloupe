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
from silverloupe.delivery import apply_delivery, rollback_delivery
from silverloupe.delivery.lightcraft import plan_lightcraft


def _asset(asset_id: str) -> Asset:
    return Asset(
        asset_id=asset_id,
        relative_path=f"{asset_id}.jpg",
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
    def __init__(self, assets: list[Asset], *, label_mapping: dict[str, str] | None = None):
        self.label_mapping = label_mapping or {}
        self.mapping = {
            asset.asset_id: {
                "asset_id": asset.asset_id,
                "photo_id": index + 201,
                "content_sha256": asset.content_sha256,
            }
            for index, asset in enumerate(assets)
        }
        self.photos = {
            index + 201: {
                "id": index + 201,
                "fileName": asset.relative_path,
                "rating": 0,
                "flag": "none",
                "label": None,
                "develop": {"light.exposure": 0.37},
            }
            for index, asset in enumerate(assets)
        }
        self.batches: list[list[dict[str, Any]]] = []
        self.fail_once: set[tuple[str, int]] = set()

    def discover(self) -> dict[str, Any]:
        return {
            "commands": ["catalog.query", "photo.rate", "photo.flag", "photo.label"],
            "label_mapping": self.label_mapping,
        }

    def resolve(self, assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.mapping[asset["asset_id"]] for asset in assets]

    def read(self, ids: list[int]) -> list[dict[str, Any]]:
        return [dict(self.photos[photo_id]) for photo_id in ids if photo_id in self.photos]

    def batch(self, operations: list[dict[str, Any]]) -> dict[str, Any]:
        self.batches.append(operations)
        results: list[dict[str, Any]] = []
        for operation in operations:
            command = operation["command"]
            ids = operation["params"]["ids"]
            assert ids
            assert command in {"photo.rate", "photo.flag", "photo.label"}
            field = "rating" if command == "photo.rate" else command.removeprefix("photo.")
            value = operation["params"][field]
            failed_ids: list[int] = []
            applied_ids: list[int] = []
            for photo_id in ids:
                key = (operation["operation_id"], photo_id)
                if key in self.fail_once:
                    self.fail_once.remove(key)
                    failed_ids.append(photo_id)
                    continue
                self.photos[photo_id][field] = (
                    None if field == "label" and value == "none" else value
                )
                applied_ids.append(photo_id)
            results.append(
                {
                    "operation_id": operation["operation_id"],
                    "applied_ids": applied_ids,
                    "failed_ids": failed_ids,
                }
            )
        return {"results": results}


def _plan(
    transport: FakeTransport,
    assets: list[Asset],
    decisions: list[Decision],
    *,
    previous=None,
):
    return plan_lightcraft(
        Manifest(assets=assets),
        [_item(asset) for asset in assets],
        decisions,
        transport,
        previous_receipt=previous,
    )


def test_apply_uses_explicit_ids_and_reads_back_without_touching_develop() -> None:
    selected = _asset("selected")
    rejected = _asset("rejected")
    transport = FakeTransport([selected, rejected], label_mapping={"top": "red"})
    develop_before = {
        photo_id: dict(photo["develop"]) for photo_id, photo in transport.photos.items()
    }
    plan = _plan(
        transport,
        [selected, rejected],
        [
            _decision(selected, SelectionTier.TOP),
            _decision(
                rejected,
                SelectionTier.NONE,
                status=DecisionStatus.REJECT,
                authority=DecisionAuthority.USER,
            ),
        ],
    )

    receipt = apply_delivery(plan, transport)

    assert receipt.status == "complete"
    assert {outcome.target_id for outcome in receipt.outcomes} == {"201", "202"}
    assert transport.photos[201]["rating"] == 5
    assert transport.photos[201]["label"] == "red"
    assert transport.photos[202]["flag"] == "reject"
    assert all(operation["params"]["ids"] for batch in transport.batches for operation in batch)
    assert {
        operation["command"] for batch in transport.batches for operation in batch
    } == {"photo.rate", "photo.flag", "photo.label"}
    assert {
        photo_id: photo["develop"] for photo_id, photo in transport.photos.items()
    } == develop_before


def test_partial_failure_resumes_and_repeated_apply_is_idempotent() -> None:
    first = _asset("first")
    second = _asset("second")
    transport = FakeTransport([first, second])
    plan = _plan(
        transport,
        [first, second],
        [
            _decision(first, SelectionTier.TOP),
            _decision(second, SelectionTier.SELECT),
        ],
    )
    second_operation = next(
        change for change in plan.proposed_changes if change.asset_id == second.asset_id
    )
    operation_id = hashlib.sha256(
        f"{plan.plan_id}\0{second.asset_id}\0rating\0{second_operation.value}".encode()
    ).hexdigest()
    transport.fail_once.add((operation_id, 202))

    partial = apply_delivery(plan, transport)
    resumed = apply_delivery(plan, transport)
    batch_count = len(transport.batches)
    repeated = apply_delivery(plan, transport)

    assert partial.status == "partial"
    assert [outcome.status for outcome in partial.outcomes] == ["applied", "failed"]
    assert resumed.status == "complete"
    assert [outcome.status for outcome in resumed.outcomes] == ["skipped", "applied"]
    assert repeated.status == "complete"
    assert [outcome.status for outcome in repeated.outcomes] == ["skipped", "skipped"]
    assert len(transport.batches) == batch_count
    assert partial.operation_ids == resumed.operation_ids == repeated.operation_ids


def test_revised_selection_restores_owned_baseline_and_rollback_restores_original() -> None:
    asset = _asset("owned")
    transport = FakeTransport([asset], label_mapping={"top": "red", "select": "green"})
    transport.photos[201].update({"rating": 2, "label": "blue"})
    # The host explicitly grants ownership of the current values for this fixture.
    transport.photos[201].update({"rating": 0, "label": None})

    first_plan = _plan(
        transport,
        [asset],
        [_decision(asset, SelectionTier.TOP)],
    )
    first = apply_delivery(first_plan, transport)
    second_plan = _plan(
        transport,
        [asset],
        [_decision(asset, SelectionTier.SELECT)],
        previous=first,
    )
    second = apply_delivery(second_plan, transport)
    rollback = rollback_delivery(second, transport)

    assert first.status == second.status == "complete"
    assert second.outcomes[0].readback_values["rating"] == 4
    assert second.outcomes[0].readback_values["label"] == "green"
    assert rollback.status == "rolled_back"
    assert transport.photos[201]["rating"] == 0
    assert transport.photos[201]["label"] is None


def test_apply_rejects_stale_preimage_without_writing() -> None:
    asset = _asset("stale")
    transport = FakeTransport([asset])
    plan = _plan(
        transport,
        [asset],
        [_decision(asset, SelectionTier.TOP)],
    )
    transport.photos[201]["rating"] = 2

    receipt = apply_delivery(plan, transport)

    assert receipt.status == "partial"
    assert receipt.outcomes[0].conflict == {"reason": "lightcraft_preimage_changed"}
    assert transport.photos[201]["rating"] == 2
    assert transport.batches == []


def test_rollback_preserves_external_edit() -> None:
    asset = _asset("external")
    transport = FakeTransport([asset])
    receipt = apply_delivery(
        _plan(
            transport,
            [asset],
            [_decision(asset, SelectionTier.TOP)],
        ),
        transport,
    )
    transport.photos[201]["rating"] = 3

    rollback = rollback_delivery(receipt, transport)

    assert rollback.status == "partial"
    assert rollback.outcomes[0].status == "skipped"
    assert rollback.outcomes[0].conflict == {"reason": "postimage_changed"}
    assert transport.photos[201]["rating"] == 3
