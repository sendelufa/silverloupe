"""Decision mapping and transaction entry points for metadata delivery."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from silverloupe.contracts import (
    Asset,
    AssetMapping,
    Decision,
    DecisionAuthority,
    DecisionStatus,
    DeliveryPlan,
    DeliveryOutcome,
    DeliveryReceipt,
    Manifest,
    ProposedChange,
    SelectionItem,
    SelectionTier,
)
from silverloupe.delivery.adobe import PROPRIETARY_RAW_EXTENSIONS, target_profile
from silverloupe.delivery.xmp import (
    _atomic_write_bytes,
    commit_staged_xmp,
    read_xmp,
    stage_xmp,
)


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


def _ownership(previous: DeliveryReceipt | None, asset_id: str) -> dict[str, Any]:
    if previous is None:
        return {}
    value = previous.field_ownership.get(asset_id, {})
    return value if isinstance(value, dict) else {}


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
        changes = _desired_changes(decision, reject_authorized=reject_authorized)
        previous_ownership = _ownership(previous_receipt, asset.asset_id)
        for field, details in previous_ownership.items():
            if field in changes or not isinstance(details, dict):
                continue
            changes[field] = [] if field == "keywords" else details.get("baseline")
        for field, value in changes.items():
            existing = existing_values.get(field)
            prior = previous_ownership.get(field)
            externally_changed = False
            if isinstance(prior, dict):
                if field == "keywords":
                    externally_changed = not set(prior.get("owned", [])).issubset(
                        set(existing or [])
                    )
                else:
                    externally_changed = existing != prior.get("last")
            elif field != "keywords" and existing is not None:
                externally_changed = True
            if externally_changed:
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
        if any(change.asset_id == asset.asset_id for change in proposed):
            mappings.append(
                AssetMapping(
                    asset_id=asset.asset_id,
                    target_id=metadata_path,
                    source_path=asset.relative_path,
                    metadata_path=metadata_path,
                    expected_before=expected_before,
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
    """Apply or resume a plan, journaling every target independently."""

    if plan.status != "ready" or plan.backend != "xmp":
        raise ValueError("only a ready XMP delivery plan can be applied")
    if plan.output_root is None or plan.source_root is None or plan.journal_path is None:
        raise ValueError("delivery plan lacks transaction roots or journal path")
    output = Path(plan.output_root).expanduser().resolve()
    journal_path = _output_path(output, plan.journal_path)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    records = _read_journal(journal_path)
    applied_by_operation = {
        str(record["operation_id"]): record
        for record in records
        if record.get("status") == "applied" and record.get("operation_id")
    }
    outcomes: list[DeliveryOutcome] = []
    ownership: dict[str, Any] = {}
    rollback_data: dict[str, Any] = {}
    backups: list[str] = []
    operation_ids: list[str] = []

    for mapping in plan.asset_mappings:
        changes = [
            change for change in plan.proposed_changes if change.asset_id == mapping.asset_id
        ]
        operation_id = _operation_id(plan, mapping, changes)
        operation_ids.append(operation_id)
        recorded = applied_by_operation.get(operation_id)
        if recorded is not None:
            conflict = _record_conflict(plan, mapping, recorded)
            if conflict is None:
                outcomes.append(
                    DeliveryOutcome(
                        asset_id=mapping.asset_id,
                        target_id=mapping.target_id,
                        operation_id=operation_id,
                        status="skipped",
                        readback_values=recorded.get("readback", {}),
                    )
                )
                ownership[mapping.asset_id] = recorded.get("ownership", {})
                rollback_data[mapping.asset_id] = recorded.get("rollback", {})
                backup = recorded.get("rollback", {}).get("backup_path")
                if backup:
                    backups.append(str(backup))
                continue
            outcomes.append(
                DeliveryOutcome(
                    asset_id=mapping.asset_id,
                    target_id=mapping.target_id,
                    operation_id=operation_id,
                    status="failed",
                    conflict=conflict,
                    error=conflict["reason"],
                )
            )
            continue
        try:
            staged = stage_xmp(plan, mapping)
            readback = commit_staged_xmp(staged)
            field_ownership = _field_ownership(plan, mapping, changes, readback)
            rollback = {
                "target_id": mapping.target_id,
                "target_existed": staged.target_existed,
                "preimage_sha256": staged.target_sha256,
                "postimage_sha256": _file_sha256(staged.target_path),
                "backup_path": (
                    staged.backup_path.relative_to(output).as_posix()
                    if staged.backup_path is not None
                    else None
                ),
                "readback": readback,
            }
            record = {
                "schema_version": 1,
                "operation_id": operation_id,
                "asset_id": mapping.asset_id,
                "target_id": mapping.target_id,
                "status": "applied",
                "readback": readback,
                "ownership": field_ownership,
                "rollback": rollback,
            }
            _append_journal(journal_path, record)
            outcomes.append(
                DeliveryOutcome(
                    asset_id=mapping.asset_id,
                    target_id=mapping.target_id,
                    operation_id=operation_id,
                    status="applied",
                    readback_values=readback,
                )
            )
            ownership[mapping.asset_id] = field_ownership
            rollback_data[mapping.asset_id] = rollback
            if rollback["backup_path"]:
                backups.append(str(rollback["backup_path"]))
        except (OSError, ValueError) as error:
            _append_journal(
                journal_path,
                {
                    "schema_version": 1,
                    "operation_id": operation_id,
                    "asset_id": mapping.asset_id,
                    "target_id": mapping.target_id,
                    "status": "failed",
                    "error": str(error),
                },
            )
            outcomes.append(
                DeliveryOutcome(
                    asset_id=mapping.asset_id,
                    target_id=mapping.target_id,
                    operation_id=operation_id,
                    status="failed",
                    error=str(error),
                )
            )

    status = "complete" if all(outcome.status != "failed" for outcome in outcomes) else "partial"
    receipt_payload = {
        "plan_id": plan.plan_id,
        "outcomes": [outcome.model_dump(mode="json") for outcome in outcomes],
        "rollback_data": rollback_data,
    }
    return DeliveryReceipt(
        target=plan.target,
        receipt_id=_plan_id(receipt_payload),
        plan_id=plan.plan_id,
        status=status,
        source_root=plan.source_root,
        output_root=plan.output_root,
        outcomes=outcomes,
        backups=list(dict.fromkeys(backups)),
        rollback_data=rollback_data,
        operation_ids=operation_ids,
        field_ownership=ownership,
    )


def rollback_delivery(receipt: DeliveryReceipt) -> DeliveryReceipt:
    """Restore only postimages that still match this receipt."""

    if receipt.source_root is None or receipt.output_root is None:
        raise ValueError("delivery receipt lacks transaction roots")
    source = Path(receipt.source_root).expanduser().resolve()
    output = Path(receipt.output_root).expanduser().resolve()
    outcomes: list[DeliveryOutcome] = []
    for asset_id, data in receipt.rollback_data.items():
        if not isinstance(data, dict) or not isinstance(data.get("target_id"), str):
            outcomes.append(
                DeliveryOutcome(
                    asset_id=asset_id,
                    status="skipped",
                    conflict={"reason": "rollback_evidence_missing"},
                    error="rollback evidence missing",
                )
            )
            continue
        target = _source_path(source, data["target_id"])
        expected_postimage = data.get("postimage_sha256")
        if not target.is_file() or _file_sha256(target) != expected_postimage:
            outcomes.append(
                DeliveryOutcome(
                    asset_id=asset_id,
                    target_id=data["target_id"],
                    status="skipped",
                    conflict={"reason": "postimage_changed"},
                    error="postimage changed after delivery",
                )
            )
            continue
        if data.get("target_existed"):
            backup_value = data.get("backup_path")
            if not isinstance(backup_value, str):
                backup = None
            else:
                backup = _output_path(output, backup_value)
            if (
                backup is None
                or not backup.is_file()
                or _file_sha256(backup) != data.get("preimage_sha256")
            ):
                outcomes.append(
                    DeliveryOutcome(
                        asset_id=asset_id,
                        target_id=data["target_id"],
                        status="skipped",
                        conflict={"reason": "backup_missing"},
                        error="verified backup is unavailable",
                    )
                )
                continue
            _atomic_write_bytes(target, backup.read_bytes())
            readback = read_xmp(target)
        else:
            target.unlink()
            readback = {"rating": None, "label": None, "keywords": []}
        outcomes.append(
            DeliveryOutcome(
                asset_id=asset_id,
                target_id=data["target_id"],
                status="applied",
                readback_values=readback,
            )
        )
    complete = all(outcome.status == "applied" for outcome in outcomes)
    payload = {
        "receipt_id": receipt.receipt_id,
        "outcomes": [outcome.model_dump(mode="json") for outcome in outcomes],
    }
    return DeliveryReceipt(
        target=receipt.target,
        receipt_id=_plan_id(payload),
        plan_id=receipt.plan_id,
        status="rolled_back" if complete else "partial",
        source_root=receipt.source_root,
        output_root=receipt.output_root,
        outcomes=outcomes,
        backups=receipt.backups,
        operation_ids=receipt.operation_ids,
    )


def _operation_id(
    plan: DeliveryPlan,
    mapping: AssetMapping,
    changes: list[ProposedChange],
) -> str:
    payload = {
        "plan_id": plan.plan_id,
        "asset_id": mapping.asset_id,
        "target_id": mapping.target_id,
        "changes": [change.model_dump(mode="json") for change in changes],
    }
    return _plan_id(payload)


def _source_path(root: Path, relative: str) -> Path:
    value = Path(relative)
    if value.is_absolute() or ".." in value.parts:
        raise ValueError(f"delivery target must be relative: {relative}")
    candidate = root / value
    if candidate.is_symlink() or not candidate.resolve().is_relative_to(root):
        raise ValueError(f"delivery target escapes source root: {relative}")
    return candidate


def _output_path(root: Path, relative: str) -> Path:
    value = Path(relative)
    if value.is_absolute() or ".." in value.parts:
        raise ValueError(f"delivery artifact must be relative: {relative}")
    candidate = root / value
    if candidate.is_symlink() or not candidate.resolve().is_relative_to(root):
        raise ValueError(f"delivery artifact escapes output root: {relative}")
    return candidate


def _read_journal(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError as error:
            raise ValueError(f"invalid delivery journal line {line_number}: {error}") from error
        if not isinstance(value, dict):
            raise ValueError(f"invalid delivery journal line {line_number}: expected object")
        records.append(value)
    return records


def _append_journal(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"delivery journal must not be a symlink: {path}")
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def _record_conflict(
    plan: DeliveryPlan,
    mapping: AssetMapping,
    record: dict[str, Any],
) -> dict[str, Any] | None:
    if plan.source_root is None:
        return {"reason": "source_root_missing"}
    target = _source_path(Path(plan.source_root).expanduser().resolve(), mapping.target_id)
    rollback = record.get("rollback", {})
    if not target.is_file() or _file_sha256(target) != rollback.get("postimage_sha256"):
        return {"reason": "postimage_changed"}
    try:
        if read_xmp(target) != record.get("readback", {}):
            return {"reason": "readback_changed"}
    except ValueError:
        return {"reason": "readback_invalid"}
    return None


def _field_ownership(
    plan: DeliveryPlan,
    mapping: AssetMapping,
    changes: list[ProposedChange],
    readback: dict[str, Any],
) -> dict[str, Any]:
    prior = plan.previous_receipt.get("field_ownership", {}).get(mapping.asset_id, {})
    prior = prior if isinstance(prior, dict) else {}
    before = mapping.expected_before.get("field_values", {})
    before = before if isinstance(before, dict) else {}
    result: dict[str, Any] = {}
    for change in changes:
        old = prior.get(change.field, {})
        old = old if isinstance(old, dict) else {}
        details: dict[str, Any] = {
            "baseline": old.get("baseline", before.get(change.field)),
            "last": readback.get(change.field),
            "target_id": mapping.target_id,
        }
        if change.field == "keywords":
            details["owned"] = list(change.value)
        result[change.field] = details
    return result
