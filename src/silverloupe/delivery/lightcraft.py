"""Host-mediated native metadata delivery for LightCraft."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from silverloupe.contracts import (
    Asset,
    AssetMapping,
    Decision,
    DecisionAuthority,
    DecisionStatus,
    DeliveryOutcome,
    DeliveryPlan,
    DeliveryReceipt,
    Manifest,
    ProposedChange,
    SelectionItem,
    SelectionTier,
)


PROFILE_VERSION = "lightcraft-mcp-v1"
_REQUIRED_COMMANDS = {"catalog.query", "photo.rate", "photo.flag", "photo.label"}
_RATING_BY_TIER = {
    SelectionTier.TOP: 5,
    SelectionTier.SELECT: 4,
    SelectionTier.RESERVE: 3,
}
_FIELDS = ("rating", "flag", "label")
_VALID_FLAGS = {"pick", "reject", "none"}
_VALID_LABELS = {"red", "yellow", "green", "blue", "purple", "none", None}


class LightCraftTransport(Protocol):
    """Explicit bridge between the Python runtime and host-owned MCP calls."""

    def discover(self) -> dict[str, Any]: ...

    def resolve(self, assets: list[dict[str, Any]]) -> list[dict[str, Any]]: ...

    def read(self, ids: list[int]) -> list[dict[str, Any]]: ...

    def batch(self, operations: list[dict[str, Any]]) -> dict[str, Any]: ...


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _item_assets(item: SelectionItem) -> list[str]:
    return item.required_asset_ids if item.kind == "composite" else [item.primary_asset_id]


def _metadata(photo: dict[str, Any]) -> dict[str, Any]:
    label = photo.get("label")
    return {
        "rating": photo.get("rating"),
        "flag": photo.get("flag"),
        "label": None if label == "none" else label,
    }


def _photo_id(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _prior_ownership(
    previous_receipt: DeliveryReceipt | None,
    asset_id: str,
    target_id: str,
) -> dict[str, dict[str, Any]]:
    if previous_receipt is None:
        return {}
    raw = previous_receipt.field_ownership.get(asset_id, {})
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for field, details in raw.items():
        if (
            field in _FIELDS
            and isinstance(details, dict)
            and details.get("target_id") == target_id
        ):
            result[field] = details
    return result


def _desired_values(
    decision: Decision,
    label_mapping: dict[str, str],
) -> dict[str, Any]:
    desired: dict[str, Any] = {}
    if decision.tier in _RATING_BY_TIER:
        desired["rating"] = _RATING_BY_TIER[decision.tier]
    if (
        decision.status == DecisionStatus.REJECT
        and decision.decision_authority
        in {DecisionAuthority.USER, DecisionAuthority.VALIDATED_POLICY}
    ):
        desired["flag"] = "reject"
    label_key = decision.tier.value
    if label_key in label_mapping:
        desired["label"] = label_mapping[label_key]
    return desired


def _unavailable_plan(reason: str, *, missing: list[str] | None = None) -> DeliveryPlan:
    conflict: dict[str, Any] = {"reason": reason, "target": "lightcraft"}
    if missing:
        conflict["missing"] = missing
    payload = {"target": "lightcraft", "conflicts": [conflict]}
    return DeliveryPlan(
        target="lightcraft",
        profile_version=PROFILE_VERSION,
        plan_id=_digest(payload),
        backend="unavailable",
        status="report_only",
        conflicts=[conflict],
    )


def plan_lightcraft(
    manifest: Manifest,
    items: list[SelectionItem],
    decisions: list[Decision],
    transport: LightCraftTransport | object,
    *,
    previous_receipt: DeliveryReceipt | None = None,
) -> DeliveryPlan:
    """Build a non-mutating LightCraft delivery plan from explicit host results."""

    if transport is None or not all(
        callable(getattr(transport, method, None))
        for method in ("discover", "resolve", "read", "batch")
    ):
        return _unavailable_plan("lightcraft_transport_unavailable")
    typed = transport  # runtime validation above keeps the public boundary object-friendly
    try:
        discovery = typed.discover()  # type: ignore[attr-defined]
    except Exception as error:
        return _unavailable_plan(f"lightcraft_discovery_failed:{error}")
    if not isinstance(discovery, dict):
        return _unavailable_plan("lightcraft_discovery_invalid")
    commands = discovery.get("commands", [])
    command_set = {value for value in commands if isinstance(value, str)}
    missing_commands = sorted(_REQUIRED_COMMANDS - command_set)
    if missing_commands:
        return _unavailable_plan(
            "lightcraft_capability_missing", missing=missing_commands
        )
    raw_label_mapping = discovery.get("label_mapping", {})
    label_mapping = raw_label_mapping if isinstance(raw_label_mapping, dict) else {}

    assets = {asset.asset_id: asset for asset in manifest.assets}
    decisions_by_item = {decision.item_id: decision for decision in decisions}
    if len(decisions_by_item) != len(decisions):
        raise ValueError("LightCraft decisions must have unique item IDs")
    missing_decisions = sorted({item.item_id for item in items} - set(decisions_by_item))
    if missing_decisions:
        raise ValueError(f"LightCraft items lack decisions: {missing_decisions}")

    candidates: list[tuple[Asset, str, Decision]] = []
    conflicts: list[dict[str, Any]] = []
    for item in items:
        decision = decisions_by_item[item.item_id]
        for asset_id in _item_assets(item):
            asset = assets.get(asset_id)
            if asset is None:
                conflicts.append(
                    {"asset_id": asset_id, "item_id": item.item_id, "reason": "unknown_asset"}
                )
                continue
            candidates.append((asset, item.item_id, decision))

    request_assets = [
        {
            "asset_id": asset.asset_id,
            "relative_path": asset.relative_path,
            "content_sha256": asset.content_sha256,
        }
        for asset, _item_id, _decision in candidates
    ]
    try:
        resolved = typed.resolve(request_assets)  # type: ignore[attr-defined]
    except Exception as error:
        return _unavailable_plan(f"lightcraft_mapping_failed:{error}")
    if not isinstance(resolved, list):
        return _unavailable_plan("lightcraft_mapping_invalid")
    mapping_by_asset = {
        value.get("asset_id"): value
        for value in resolved
        if isinstance(value, dict) and isinstance(value.get("asset_id"), str)
    }
    target_to_assets: dict[int, list[str]] = {}
    valid: list[tuple[Asset, str, Decision, int]] = []
    for asset, item_id, decision in candidates:
        mapping = mapping_by_asset.get(asset.asset_id)
        if mapping is None:
            conflicts.append(
                {
                    "asset_id": asset.asset_id,
                    "item_id": item_id,
                    "reason": "lightcraft_mapping_missing",
                }
            )
            continue
        target_id = _photo_id(mapping.get("photo_id"))
        if (
            target_id is None
            or mapping.get("content_sha256") != asset.content_sha256
        ):
            conflicts.append(
                {
                    "asset_id": asset.asset_id,
                    "item_id": item_id,
                    "reason": "lightcraft_mapping_stale",
                }
            )
            continue
        target_to_assets.setdefault(target_id, []).append(asset.asset_id)
        valid.append((asset, item_id, decision, target_id))

    duplicate_targets = {
        target_id: asset_ids
        for target_id, asset_ids in target_to_assets.items()
        if len(asset_ids) > 1
    }
    duplicate_assets = {
        asset_id for asset_ids in duplicate_targets.values() for asset_id in asset_ids
    }
    for target_id, asset_ids in duplicate_targets.items():
        conflicts.append(
            {
                "target_id": str(target_id),
                "asset_ids": asset_ids,
                "reason": "lightcraft_mapping_collision",
            }
        )
    valid = [row for row in valid if row[0].asset_id not in duplicate_assets]
    target_ids = [target_id for _asset, _item_id, _decision, target_id in valid]
    try:
        photos = typed.read(target_ids) if target_ids else []  # type: ignore[attr-defined]
    except Exception as error:
        return _unavailable_plan(f"lightcraft_read_failed:{error}")
    if not isinstance(photos, list):
        return _unavailable_plan("lightcraft_read_invalid")
    photos_by_id = {
        photo_id: value
        for value in photos
        if isinstance(value, dict) and (photo_id := _photo_id(value.get("id"))) is not None
    }

    mappings: list[AssetMapping] = []
    changes: list[ProposedChange] = []
    for asset, item_id, decision, target_id in valid:
        photo = photos_by_id.get(target_id)
        if photo is None:
            conflicts.append(
                {
                    "asset_id": asset.asset_id,
                    "item_id": item_id,
                    "target_id": str(target_id),
                    "reason": "lightcraft_mapping_stale",
                }
            )
            continue
        before = _metadata(photo)
        target_value = str(target_id)
        prior = _prior_ownership(previous_receipt, asset.asset_id, target_value)
        desired = _desired_values(decision, label_mapping)
        for field, details in prior.items():
            if field not in desired:
                desired[field] = details.get("baseline")

        if before["flag"] not in _VALID_FLAGS:
            conflicts.append(
                {
                    "asset_id": asset.asset_id,
                    "target_id": target_value,
                    "field": "flag",
                    "reason": "lightcraft_unknown_field_value",
                    "value": before["flag"],
                }
            )
        if before["label"] not in _VALID_LABELS:
            conflicts.append(
                {
                    "asset_id": asset.asset_id,
                    "target_id": target_value,
                    "field": "label",
                    "reason": "lightcraft_unknown_field_value",
                    "value": before["label"],
                }
            )

        mappings.append(
            AssetMapping(
                asset_id=asset.asset_id,
                target_id=target_value,
                source_path=asset.relative_path,
                expected_before={
                    "content_sha256": asset.content_sha256,
                    "field_values": before,
                },
            )
        )
        for field, value in desired.items():
            if field == "label" and (value not in _VALID_LABELS or before[field] not in _VALID_LABELS):
                continue
            if field == "flag" and (value not in _VALID_FLAGS or before[field] not in _VALID_FLAGS):
                continue
            ownership = prior.get(field)
            current = before[field]
            if ownership is not None:
                if current != ownership.get("last"):
                    conflicts.append(
                        {
                            "asset_id": asset.asset_id,
                            "target_id": target_value,
                            "field": field,
                            "reason": "lightcraft_owned_field_changed",
                        }
                    )
                    continue
            elif current not in {0, "none", None} and current != value:
                conflicts.append(
                    {
                        "asset_id": asset.asset_id,
                        "target_id": target_value,
                        "field": field,
                        "reason": "lightcraft_user_field_preserved",
                    }
                )
                continue
            if current != value:
                changes.append(
                    ProposedChange(
                        asset_id=asset.asset_id,
                        item_id=item_id,
                        field=field,
                        value=value,
                    )
                )

    payload = {
        "target": "lightcraft",
        "profile_version": PROFILE_VERSION,
        "mappings": [mapping.model_dump(mode="json") for mapping in mappings],
        "changes": [change.model_dump(mode="json") for change in changes],
    }
    return DeliveryPlan(
        target="lightcraft",
        profile_version=PROFILE_VERSION,
        plan_id=_digest(payload),
        backend="native" if mappings else "unavailable",
        status="ready" if mappings else "report_only",
        previous_receipt=(
            previous_receipt.model_dump(mode="json") if previous_receipt else {}
        ),
        asset_mappings=mappings,
        proposed_changes=changes,
        conflicts=conflicts,
    )


def _operation_id(plan: DeliveryPlan, change: ProposedChange) -> str:
    return hashlib.sha256(
        f"{plan.plan_id}\0{change.asset_id}\0{change.field}\0{change.value}".encode()
    ).hexdigest()


def _command_value(field: str, value: Any) -> Any:
    if field in {"flag", "label"} and value is None:
        return "none"
    return value


def _command_name(field: str) -> str:
    return "photo.rate" if field == "rating" else f"photo.{field}"


def _ownership(
    plan: DeliveryPlan,
    mapping: AssetMapping,
    fields: dict[str, Any],
) -> dict[str, Any]:
    previous = plan.previous_receipt.get("field_ownership", {}).get(mapping.asset_id, {})
    previous = previous if isinstance(previous, dict) else {}
    before = mapping.expected_before.get("field_values", {})
    before = before if isinstance(before, dict) else {}
    return {
        field: {
            "baseline": (
                previous.get(field, {}).get("baseline", before.get(field))
                if isinstance(previous.get(field, {}), dict)
                else before.get(field)
            ),
            "last": value,
            "target_id": mapping.target_id,
        }
        for field, value in fields.items()
    }


def apply_lightcraft(
    plan: DeliveryPlan,
    transport: LightCraftTransport,
) -> DeliveryReceipt:
    """Apply a ready native plan and verify every asset through readback."""

    if plan.target != "lightcraft" or plan.backend != "native" or plan.status != "ready":
        raise ValueError("only a ready LightCraft native plan can be applied")
    mapping_by_asset = {mapping.asset_id: mapping for mapping in plan.asset_mappings}
    changes_by_asset: dict[str, list[ProposedChange]] = {
        mapping.asset_id: [] for mapping in plan.asset_mappings
    }
    for change in plan.proposed_changes:
        changes_by_asset.setdefault(change.asset_id, []).append(change)
    ids = [int(mapping.target_id) for mapping in plan.asset_mappings]
    current_rows = transport.read(ids)
    current = {
        int(row["id"]): _metadata(row)
        for row in current_rows
        if isinstance(row, dict) and _photo_id(row.get("id")) is not None
    }

    operations: list[dict[str, Any]] = []
    failed_assets: dict[str, dict[str, Any]] = {}
    skipped_assets: set[str] = set()
    operation_ids = [_operation_id(plan, change) for change in plan.proposed_changes]
    operation_ids_by_asset = {
        asset_id: [
            _operation_id(plan, change) for change in changes_by_asset[asset_id]
        ]
        for asset_id in changes_by_asset
    }
    desired_by_asset: dict[str, dict[str, Any]] = {}
    for asset_id, mapping in mapping_by_asset.items():
        photo_id = int(mapping.target_id)
        actual = current.get(photo_id)
        desired = {change.field: change.value for change in changes_by_asset[asset_id]}
        desired_by_asset[asset_id] = desired
        if actual is None:
            failed_assets[asset_id] = {"reason": "lightcraft_mapping_stale"}
            continue
        expected = mapping.expected_before.get("field_values", {})
        pending: list[ProposedChange] = []
        for change in changes_by_asset[asset_id]:
            if actual.get(change.field) == change.value:
                continue
            if actual.get(change.field) != expected.get(change.field):
                failed_assets[asset_id] = {"reason": "lightcraft_preimage_changed"}
                pending = []
                break
            pending.append(change)
        if asset_id in failed_assets:
            continue
        if not pending:
            skipped_assets.add(asset_id)
        for change in pending:
            operation_id = _operation_id(plan, change)
            operations.append(
                {
                    "operation_id": operation_id,
                    "asset_id": asset_id,
                    "command": _command_name(change.field),
                    "params": {
                        change.field: _command_value(change.field, change.value),
                        "ids": [photo_id],
                    },
                }
            )

    failed_operations: set[str] = set()
    if operations:
        response = transport.batch(operations)
        results = response.get("results", []) if isinstance(response, dict) else []
        result_by_id = {
            result.get("operation_id"): result
            for result in results
            if isinstance(result, dict) and isinstance(result.get("operation_id"), str)
        }
        for operation in operations:
            result = result_by_id.get(operation["operation_id"], {})
            photo_id = operation["params"]["ids"][0]
            if photo_id not in result.get("applied_ids", []):
                failed_operations.add(operation["operation_id"])

    readback_rows = transport.read(ids)
    readback = {
        int(row["id"]): _metadata(row)
        for row in readback_rows
        if isinstance(row, dict) and _photo_id(row.get("id")) is not None
    }
    outcomes: list[DeliveryOutcome] = []
    field_ownership: dict[str, Any] = {}
    rollback_data: dict[str, Any] = {}
    for mapping in plan.asset_mappings:
        asset_id = mapping.asset_id
        photo_id = int(mapping.target_id)
        values = readback.get(photo_id, {})
        conflict = failed_assets.get(asset_id)
        asset_operations = [
            operation for operation in operations if operation["asset_id"] == asset_id
        ]
        if any(operation["operation_id"] in failed_operations for operation in asset_operations):
            conflict = {"reason": "lightcraft_operation_failed"}
        desired = desired_by_asset[asset_id]
        if conflict is None and any(values.get(field) != value for field, value in desired.items()):
            conflict = {"reason": "lightcraft_readback_mismatch"}
        if conflict is not None:
            outcomes.append(
                DeliveryOutcome(
                    asset_id=asset_id,
                    target_id=mapping.target_id,
                    status="failed",
                    conflict=conflict,
                    error=conflict["reason"],
                    readback_values=values,
                )
            )
            continue
        owned = _ownership(plan, mapping, desired)
        field_ownership[asset_id] = owned
        rollback_data[asset_id] = {
            "target_id": mapping.target_id,
            "baseline": {field: details["baseline"] for field, details in owned.items()},
            "postimage": desired,
        }
        outcomes.append(
            DeliveryOutcome(
                asset_id=asset_id,
                target_id=mapping.target_id,
                operation_id=(
                    operation_ids_by_asset[asset_id][-1]
                    if operation_ids_by_asset[asset_id]
                    else None
                ),
                status="skipped" if asset_id in skipped_assets else "applied",
                readback_values=values,
            )
        )

    status = "complete" if all(outcome.status != "failed" for outcome in outcomes) else "partial"
    receipt_payload = {
        "plan_id": plan.plan_id,
        "outcomes": [outcome.model_dump(mode="json") for outcome in outcomes],
        "rollback_data": rollback_data,
    }
    return DeliveryReceipt(
        target="lightcraft",
        receipt_id=_digest(receipt_payload),
        plan_id=plan.plan_id,
        status=status,
        outcomes=outcomes,
        rollback_data=rollback_data,
        operation_ids=operation_ids,
        field_ownership=field_ownership,
    )


def rollback_lightcraft(
    receipt: DeliveryReceipt,
    transport: LightCraftTransport,
) -> DeliveryReceipt:
    """Restore owned values only while their recorded postimage still matches."""

    if receipt.target != "lightcraft":
        raise ValueError("only a LightCraft receipt can use native rollback")
    ids = [
        int(data["target_id"])
        for data in receipt.rollback_data.values()
        if isinstance(data, dict) and str(data.get("target_id", "")).isdigit()
    ]
    rows = transport.read(ids)
    current = {
        int(row["id"]): _metadata(row)
        for row in rows
        if isinstance(row, dict) and _photo_id(row.get("id")) is not None
    }
    operations: list[dict[str, Any]] = []
    conflicts: dict[str, dict[str, Any]] = {}
    for asset_id, data in receipt.rollback_data.items():
        if not isinstance(data, dict) or not str(data.get("target_id", "")).isdigit():
            conflicts[asset_id] = {"reason": "rollback_evidence_missing"}
            continue
        photo_id = int(data["target_id"])
        postimage = data.get("postimage", {})
        baseline = data.get("baseline", {})
        if not isinstance(postimage, dict) or not isinstance(baseline, dict):
            conflicts[asset_id] = {"reason": "rollback_evidence_missing"}
            continue
        values = current.get(photo_id)
        if values is None or any(values.get(field) != value for field, value in postimage.items()):
            conflicts[asset_id] = {"reason": "postimage_changed"}
            continue
        for field, value in baseline.items():
            operations.append(
                {
                    "operation_id": _digest(
                        {
                            "receipt_id": receipt.receipt_id,
                            "asset_id": asset_id,
                            "field": field,
                            "value": value,
                        }
                    ),
                    "asset_id": asset_id,
                    "command": _command_name(field),
                    "params": {field: _command_value(field, value), "ids": [photo_id]},
                }
            )
    failed: set[str] = set()
    if operations:
        response = transport.batch(operations)
        results = response.get("results", []) if isinstance(response, dict) else []
        result_by_id = {
            result.get("operation_id"): result
            for result in results
            if isinstance(result, dict) and isinstance(result.get("operation_id"), str)
        }
        for operation in operations:
            result = result_by_id.get(operation["operation_id"], {})
            if operation["params"]["ids"][0] not in result.get("applied_ids", []):
                failed.add(operation["asset_id"])
    final_rows = transport.read(ids)
    final = {
        int(row["id"]): _metadata(row)
        for row in final_rows
        if isinstance(row, dict) and _photo_id(row.get("id")) is not None
    }
    outcomes: list[DeliveryOutcome] = []
    for asset_id, data in receipt.rollback_data.items():
        conflict = conflicts.get(asset_id)
        if asset_id in failed:
            conflict = {"reason": "lightcraft_operation_failed"}
        target_id = str(data.get("target_id")) if isinstance(data, dict) else None
        if conflict is not None:
            outcomes.append(
                DeliveryOutcome(
                    asset_id=asset_id,
                    target_id=target_id,
                    status="skipped",
                    conflict=conflict,
                    error=conflict["reason"],
                )
            )
            continue
        baseline = data.get("baseline", {})
        values = final.get(int(target_id or "0"), {})
        if any(values.get(field) != value for field, value in baseline.items()):
            conflict = {"reason": "lightcraft_readback_mismatch"}
            outcomes.append(
                DeliveryOutcome(
                    asset_id=asset_id,
                    target_id=target_id,
                    status="skipped",
                    conflict=conflict,
                    error=conflict["reason"],
                    readback_values=values,
                )
            )
            continue
        outcomes.append(
            DeliveryOutcome(
                asset_id=asset_id,
                target_id=target_id,
                status="applied",
                readback_values=values,
            )
        )
    complete = all(outcome.status == "applied" for outcome in outcomes)
    payload = {
        "receipt_id": receipt.receipt_id,
        "outcomes": [outcome.model_dump(mode="json") for outcome in outcomes],
    }
    return DeliveryReceipt(
        target="lightcraft",
        receipt_id=_digest(payload),
        plan_id=receipt.plan_id,
        status="rolled_back" if complete else "partial",
        outcomes=outcomes,
        operation_ids=receipt.operation_ids,
    )
