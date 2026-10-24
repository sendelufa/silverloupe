"""Decision mapping and transaction entry points for metadata delivery."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from silverloupe.contracts import (
    Asset,
    AssetMapping,
    Decision,
    DecisionAuthority,
    DecisionStatus,
    DeliveryPlan,
    DeliveryReceipt,
    Manifest,
    ProposedChange,
    SelectionItem,
    SelectionTier,
)
from silverloupe.delivery.adobe import PROPRIETARY_RAW_EXTENSIONS, target_profile
from silverloupe.delivery.xmp import read_xmp


_RATING_BY_TIER = {
    SelectionTier.TOP: 5,
    SelectionTier.SELECT: 4,
    SelectionTier.RESERVE: 3,
}
_KEYWORD_BY_TIER = {
    SelectionTier.TOP: "silverloupe:top",
    SelectionTier.SELECT: "silverloupe:select",
    SelectionTier.RESERVE: "silverloupe:reserve",
}


def reject_batch_hash(decisions: list[Decision]) -> str:
    """Bind one approval to the exact displayed set of proposed rejects."""

    payload = [
        {
            "item_id": decision.item_id,
            "status": decision.status.value,
            "reason_codes": sorted(decision.reason_codes),
            "provenance": decision.provenance,
        }
        for decision in sorted(decisions, key=lambda value: value.item_id)
        if decision.status == DecisionStatus.REJECT
        and decision.decision_authority == DecisionAuthority.PROPOSAL
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _owned(previous: DeliveryReceipt | None, asset_id: str, field: str) -> bool:
    if previous is None:
        return False
    ownership = previous.field_ownership.get(asset_id, {})
    return field in ownership if isinstance(ownership, dict) else False


def _item_assets(item: SelectionItem) -> list[str]:
    if item.kind == "composite":
        return item.required_asset_ids
    return [item.primary_asset_id]


def _desired_changes(
    decision: Decision,
    *,
    reject_authorized: bool,
) -> dict[str, Any]:
    if decision.status == DecisionStatus.REJECT:
        if (
            decision.decision_authority
            in {DecisionAuthority.USER, DecisionAuthority.VALIDATED_POLICY}
            or reject_authorized
        ):
            return {"rating": -1, "keywords": ["silverloupe:reject"]}
        return {"keywords": ["silverloupe:suggested-reject"]}
    changes: dict[str, Any] = {}
    if decision.tier in _RATING_BY_TIER:
        changes["rating"] = _RATING_BY_TIER[decision.tier]
        changes["keywords"] = [_KEYWORD_BY_TIER[decision.tier]]
    elif decision.status == DecisionStatus.REVIEW:
        changes["keywords"] = ["silverloupe:review"]
    return changes


def _plan_id(plan_payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(plan_payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def plan_delivery(
    manifest: Manifest,
    items: list[SelectionItem],
    decisions: list[Decision],
    target: str,
    *,
    source_root: Path | None = None,
    output_root: Path | None = None,
    previous_receipt: DeliveryReceipt | None = None,
    accepted_reject_hash: str | None = None,
) -> DeliveryPlan:
    """Create a deterministic, non-mutating metadata transaction plan."""

    profile = target_profile(target)
    source = source_root.expanduser().resolve() if source_root is not None else None
    output = output_root.expanduser().resolve() if output_root is not None else None
    assets = {asset.asset_id: asset for asset in manifest.assets}
    decision_by_item = {decision.item_id: decision for decision in decisions}
    conflicts: list[dict[str, Any]] = []

    if len(decision_by_item) != len(decisions):
        raise ValueError("delivery decisions must have unique item IDs")
    missing = sorted({item.item_id for item in items} - set(decision_by_item))
    if missing:
        raise ValueError(f"delivery items lack decisions: {missing}")

    expected_reject_hash = reject_batch_hash(decisions)
    reject_authorized = accepted_reject_hash == expected_reject_hash
    if accepted_reject_hash is not None and not reject_authorized:
        conflicts.append(
            {
                "reason": "reject_acceptance_hash_mismatch",
                "expected": expected_reject_hash,
                "provided": accepted_reject_hash,
            }
        )

    if not profile.verified:
        for item in items:
            for asset_id in _item_assets(item):
                conflicts.append(
                    {
                        "asset_id": asset_id,
                        "item_id": item.item_id,
                        "reason": profile.reason,
                        "target": target,
                    }
                )
        payload = {
            "target": target,
            "backend": profile.backend,
            "conflicts": conflicts,
        }
        return DeliveryPlan(
            target=target,
            profile_version="adobe-xmp-v1",
            plan_id=_plan_id(payload),
            backend=profile.backend,
            status="report_only",
            source_root=str(source) if source else None,
            output_root=str(output) if output else None,
            accepted_reject_hash=accepted_reject_hash,
            conflicts=conflicts,
        )

    candidates: list[tuple[SelectionItem, Decision, Asset, str]] = []
    targets_to_assets: dict[str, list[str]] = {}
    for item in items:
        decision = decision_by_item[item.item_id]
        for asset_id in _item_assets(item):
            asset = assets.get(asset_id)
            if asset is None:
                conflicts.append(
                    {"asset_id": asset_id, "item_id": item.item_id, "reason": "unknown_asset"}
                )
                continue
            path = Path(asset.relative_path)
            if path.suffix.lower() not in PROPRIETARY_RAW_EXTENSIONS:
                conflicts.append(
                    {
                        "asset_id": asset_id,
                        "item_id": item.item_id,
                        "reason": "format_not_verified_for_sidecar",
                        "format": path.suffix.lower(),
                    }
                )
                continue
            metadata_path = path.with_suffix(".xmp").as_posix()
            targets_to_assets.setdefault(metadata_path.casefold(), []).append(asset_id)
            candidates.append((item, decision, asset, metadata_path))

    collisions = {
        target_key: asset_ids
        for target_key, asset_ids in targets_to_assets.items()
        if len(asset_ids) > 1
    }
    collided_ids = {asset_id for asset_ids in collisions.values() for asset_id in asset_ids}
    for target_key, asset_ids in collisions.items():
        for asset_id in asset_ids:
            conflicts.append(
                {
                    "asset_id": asset_id,
                    "reason": "sidecar_path_collision",
                    "metadata_path": target_key,
                }
            )

    mappings: list[AssetMapping] = []
    proposed: list[ProposedChange] = []
    for item, decision, asset, metadata_path in candidates:
        if asset.asset_id in collided_ids:
            continue
        existing_values = dict(asset.existing_metadata)
        expected_before: dict[str, Any] = {
            "source_sha256": asset.content_sha256,
            "metadata_exists": False,
            "metadata_sha256": None,
            "field_values": existing_values,
        }
        if source is not None:
            source_path = source / asset.relative_path
            metadata_file = source / metadata_path
            if (
                source_path.is_symlink()
                or not source_path.is_file()
                or not source_path.resolve().is_relative_to(source)
                or _file_sha256(source_path) != asset.content_sha256
            ):
                conflicts.append(
                    {
                        "asset_id": asset.asset_id,
                        "item_id": item.item_id,
                        "reason": "source_preimage_changed",
                    }
                )
                continue
            if metadata_file.is_symlink():
                conflicts.append(
                    {
                        "asset_id": asset.asset_id,
                        "item_id": item.item_id,
                        "reason": "unsafe_metadata_symlink",
                    }
                )
                continue
            if metadata_file.is_file():
                try:
                    existing_values = read_xmp(metadata_file)
                except ValueError as error:
                    conflicts.append(
                        {
                            "asset_id": asset.asset_id,
                            "item_id": item.item_id,
                            "reason": "invalid_xmp_preimage",
                            "error": str(error),
                        }
                    )
                    continue
                expected_before.update(
                    {
                        "metadata_exists": True,
                        "metadata_sha256": _file_sha256(metadata_file),
                        "field_values": existing_values,
                    }
                )
        mappings.append(
            AssetMapping(
                asset_id=asset.asset_id,
                target_id=metadata_path,
                source_path=asset.relative_path,
                metadata_path=metadata_path,
                expected_before=expected_before,
            )
        )
        changes = _desired_changes(decision, reject_authorized=reject_authorized)
        for field, value in changes.items():
            existing = existing_values.get(field)
            if field != "keywords" and existing is not None and not _owned(
                previous_receipt, asset.asset_id, field
            ):
                conflicts.append(
                    {
                        "asset_id": asset.asset_id,
                        "item_id": item.item_id,
                        "field": field,
                        "reason": "user_value_not_owned",
                        "current": existing,
                        "proposed": value,
                    }
                )
                continue
            proposed.append(
                ProposedChange(
                    asset_id=asset.asset_id,
                    item_id=item.item_id,
                    field=field,
                    value=value,
                )
            )

    payload = {
        "target": target,
        "profile_version": "adobe-xmp-v1",
        "mappings": [mapping.model_dump(mode="json") for mapping in mappings],
        "changes": [change.model_dump(mode="json") for change in proposed],
        "accepted_reject_hash": accepted_reject_hash,
    }
    plan_id = _plan_id(payload)
    return DeliveryPlan(
        target=target,
        profile_version="adobe-xmp-v1",
        plan_id=plan_id,
        backend=profile.backend,
        status="ready" if mappings else "report_only",
        source_root=str(source) if source else None,
        output_root=str(output) if output else None,
        journal_path=f"delivery/{plan_id}/journal.jsonl" if output else None,
        accepted_reject_hash=accepted_reject_hash,
        previous_receipt=(previous_receipt.model_dump(mode="json") if previous_receipt else {}),
        asset_mappings=mappings,
        proposed_changes=proposed,
        conflicts=conflicts,
    )


def apply_delivery(plan: DeliveryPlan) -> DeliveryReceipt:
    """Apply a prepared delivery plan (implemented by the transaction task)."""

    raise NotImplementedError("delivery apply is not implemented yet")


def rollback_delivery(receipt: DeliveryReceipt) -> DeliveryReceipt:
    """Rollback one receipt (implemented by the transaction task)."""

    raise NotImplementedError("delivery rollback is not implemented yet")
