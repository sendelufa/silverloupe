"""Deterministic artifact validation and grading for development evals."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, TypeVar

from pydantic import BaseModel, ValidationError

from silverloupe.contracts import (
    Assessment,
    Decision,
    DeliveryPlan,
    Manifest,
    Selection,
    SelectionItem,
)


ModelT = TypeVar("ModelT", bound=BaseModel)


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _load_jsonl(path: Path) -> list[Any]:
    values: list[Any] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line.strip():
                try:
                    values.append(json.loads(line))
                except json.JSONDecodeError as error:
                    raise ValueError(f"{path.name}:{line_number}: invalid JSON: {error.msg}") from error
    return values


def _models(model: type[ModelT], values: Iterable[Any], source: str, errors: list[str]) -> list[ModelT]:
    parsed: list[ModelT] = []
    for index, value in enumerate(values):
        try:
            parsed.append(model.model_validate(value))
        except ValidationError as error:
            errors.append(f"{source}[{index}]: {error}")
    return parsed


def _load_required(path: Path, loader: Any, errors: list[str]) -> Any | None:
    if not path.is_file():
        errors.append(f"missing required artifact: {path.name}")
        return None
    try:
        return loader(path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        errors.append(f"{path.name}: {error}")
        return None


def _parsed_run(path: Path) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if not path.is_dir():
        return {}, [f"run path is not a directory: {path}"]

    assets_path = path / "assets.jsonl"
    legacy_manifest_path = path / "manifest.json"
    if assets_path.is_file():
        assets_value = _load_required(assets_path, _load_jsonl, errors)
        manifest_value = {"schema_version": 1, "assets": assets_value or []}
    elif legacy_manifest_path.is_file():
        manifest_value = _load_required(legacy_manifest_path, _load_json, errors)
    else:
        errors.append("missing required artifact: assets.jsonl")
        manifest_value = None
    items_value = _load_required(path / "selection-items.json", _load_json, errors)
    assessments_value = _load_required(path / "assessments.jsonl", _load_jsonl, errors)
    decisions_value = _load_required(path / "decisions.jsonl", _load_jsonl, errors)
    selection_value = _load_required(path / "selection.json", _load_json, errors)

    manifest: Manifest | None = None
    selection: Selection | None = None
    if manifest_value is not None:
        try:
            manifest = Manifest.model_validate(manifest_value)
        except ValidationError as error:
            errors.append(f"manifest.json: {error}")
    if selection_value is not None:
        try:
            selection = Selection.model_validate(selection_value)
        except ValidationError as error:
            errors.append(f"selection.json: {error}")

    items = _models(SelectionItem, items_value or [], "selection-items.json", errors)
    assessments = _models(Assessment, assessments_value or [], "assessments.jsonl", errors)
    decisions = _models(Decision, decisions_value or [], "decisions.jsonl", errors)

    delivery_plan: DeliveryPlan | None = None
    delivery_path = path / "delivery-plan.json"
    if delivery_path.is_file():
        try:
            delivery_plan = DeliveryPlan.model_validate(_load_json(delivery_path))
        except (ValidationError, OSError, ValueError, json.JSONDecodeError) as error:
            errors.append(f"delivery-plan.json: {error}")

    asset_ids = {asset.asset_id for asset in manifest.assets} if manifest else set()
    item_ids = {item.item_id for item in items}
    if len(item_ids) != len(items):
        errors.append("selection-items.json: duplicate item_id values")

    for item in items:
        for asset_id in item.member_asset_ids:
            if asset_id not in asset_ids:
                errors.append(f"selection item '{item.item_id}' references unknown asset_id '{asset_id}'")
    for assessment in assessments:
        if assessment.asset_id not in asset_ids:
            errors.append(f"assessment references unknown asset_id '{assessment.asset_id}'")
    for decision in decisions:
        if decision.item_id not in item_ids:
            errors.append(f"decision references unknown item_id '{decision.item_id}'")
    if selection:
        for field_name in ("selects_ids", "top_ids", "ranked_ids"):
            for item_id in getattr(selection, field_name):
                if item_id not in item_ids:
                    errors.append(f"selection.{field_name} references unknown item_id '{item_id}'")
    if delivery_plan:
        for mapping in delivery_plan.asset_mappings:
            if mapping.asset_id not in asset_ids:
                errors.append(f"delivery mapping references unknown asset_id '{mapping.asset_id}'")
        for change in delivery_plan.proposed_changes:
            if change.asset_id not in asset_ids:
                errors.append(f"delivery change references unknown asset_id '{change.asset_id}'")
            if change.item_id is not None and change.item_id not in item_ids:
                errors.append(f"delivery change references unknown item_id '{change.item_id}'")

    return {
        "manifest": manifest,
        "items": items,
        "assessments": assessments,
        "decisions": decisions,
        "selection": selection,
        "delivery_plan": delivery_plan,
    }, errors


def validate_run(path: Path) -> list[str]:
    """Return deterministic validation errors for a run artifact directory."""

    _, errors = _parsed_run(path)
    return errors


def _check(passed: bool, message: str, **details: Any) -> dict[str, Any]:
    return {"passed": passed, "message": message, **details}


def grade_artifacts(case: dict[str, Any], output: Path) -> dict[str, Any]:
    """Grade contract, selection-size, and rejection-safety invariants."""

    parsed, errors = _parsed_run(output)
    items: list[SelectionItem] = parsed.get("items", [])
    manifest: Manifest | None = parsed.get("manifest")
    assessments: list[Assessment] = parsed.get("assessments", [])
    decisions: list[Decision] = parsed.get("decisions", [])
    selection: Selection | None = parsed.get("selection")
    delivery_plan: DeliveryPlan | None = parsed.get("delivery_plan")

    top_ids = selection.top_ids if selection else []
    expected_k = case.get("brief", {}).get("top_k")
    requested_k_passed = expected_k is None or len(top_ids) == expected_k

    rejected = {decision.item_id for decision in decisions if decision.status.value == "reject"}
    all_item_ids = {item.item_id for item in items}
    reject_all = bool(all_item_ids) and all_item_ids.issubset(rejected)

    proposed_rejects = {
        decision.item_id
        for decision in decisions
        if decision.status.value == "reject" and decision.decision_authority.value == "proposal"
    }
    items_by_id = {item.item_id: item for item in items}
    proposed_reject_assets = {
        asset_id
        for item_id in proposed_rejects
        for asset_id in items_by_id[item_id].member_asset_ids
        if item_id in items_by_id
    }
    exported_proposed_rejects: list[str] = []
    if delivery_plan:
        for change in delivery_plan.proposed_changes:
            is_reject_field = change.field in {"reject", "reject_flag", "pick_status"}
            is_reject_value = change.value is True or str(change.value).lower() in {"reject", "rejected"}
            belongs_to_proposed_reject = (
                change.item_id in proposed_rejects
                or (change.item_id is None and change.asset_id in proposed_reject_assets)
            )
            if belongs_to_proposed_reject and is_reject_field and is_reject_value:
                exported_proposed_rejects.append(change.item_id or change.asset_id)

    gold_good = set(case.get("gold_good_item_ids", []))
    rejected_gold = sorted(gold_good & rejected)
    manifest_asset_ids = {asset.asset_id for asset in manifest.assets} if manifest else set()
    assessed_asset_ids = [assessment.asset_id for assessment in assessments]
    decided_item_ids = [decision.item_id for decision in decisions]
    missing_assessments = sorted(manifest_asset_ids - set(assessed_asset_ids))
    missing_decisions = sorted(all_item_ids - set(decided_item_ids))
    complete_accounting = (
        not missing_assessments
        and not missing_decisions
        and len(assessed_asset_ids) == len(set(assessed_asset_ids))
        and len(decided_item_ids) == len(set(decided_item_ids))
    )

    checks = {
        "valid_references": _check(not errors, "all artifact references and schemas are valid"),
        "requested_k": _check(
            requested_k_passed,
            "top size is counted in SelectionItem IDs",
            actual=len(top_ids),
            expected=expected_k,
        ),
        "complete_accounting": _check(
            complete_accounting,
            "every asset has one assessment and every selection item has one decision",
            missing_asset_ids=missing_assessments,
            missing_item_ids=missing_decisions,
        ),
        "no_reject_all": _check(
            bool(case.get("allow_reject_all")) or not reject_all,
            "the output must not reject every selection item unless the case explicitly permits it",
            rejected=len(rejected),
            total=len(all_item_ids),
        ),
        "proposed_reject_not_exported": _check(
            not exported_proposed_rejects,
            "proposal-authority rejects must not be exported as reject flags",
            item_ids=sorted(set(exported_proposed_rejects)),
        ),
        "gold_good_retained": _check(
            not rejected_gold,
            "gold-good items must not be rejected",
            item_ids=rejected_gold,
        ),
    }
    failed_messages = [check["message"] for check in checks.values() if not check["passed"]]
    return {
        "case_id": case.get("case_id"),
        "passed": all(check["passed"] for check in checks.values()),
        "checks": checks,
        "errors": [*errors, *failed_messages],
    }
