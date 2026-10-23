"""Explainable, recommendation-only technical assessment and triage."""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from silverloupe.contracts import (
    Assessment,
    Asset,
    Brief,
    Decision,
    DecisionAuthority,
    DecisionStatus,
    Group,
    Manifest,
    RunConfig,
    SelectionItem,
    SelectionTier,
)
from silverloupe.previews import validate_output_layout
from silverloupe.signals import measure_asset_signals
from silverloupe.state import atomic_write_text, load_run_config
from silverloupe.vision.base import VisionImportResult, VisionRequest
from silverloupe.vision.host_exchange import (
    apply_vision_response,
    build_vision_request,
    parse_vision_response,
)


ModelT = TypeVar("ModelT", bound=BaseModel)


def assess(
    manifest: Manifest,
    groups: list[Group],
    brief: Brief,
    config: RunConfig,
) -> list[Assessment]:
    """Return asset-level numeric assessment without unobserved visual claims."""

    known_assets = {asset.asset_id for asset in manifest.assets}
    protected_members: set[str] = set()
    for group in groups:
        unknown = set(group.member_ids) - known_assets
        if unknown:
            raise ValueError(f"group {group.group_id!r} references unknown assets: {sorted(unknown)}")
        if group.protected_set:
            protected_members.update(group.member_ids)

    assessments: list[Assessment] = []
    for asset in manifest.assets:
        result = measure_asset_signals(asset, brief, config)
        uncertainty = list(result.uncertainty)
        if asset.asset_id in protected_members:
            uncertainty.append("protected_composite_member")
        assessments.append(
            Assessment(
                asset_id=asset.asset_id,
                evaluation_status=result.evaluation_status,
                technical_signals=result.technical_signals,
                visual_status="not_evaluated",
                uncertainty=sorted(set(uncertainty)),
            )
        )
    return assessments


def _visual_concerns(assessment: Assessment) -> list[str]:
    concerns: list[str] = []
    for criterion, value in assessment.visual_criteria.items():
        normalized = str(value).strip().casefold()
        if normalized in {"concern", "uncertain", "insufficient_evidence"}:
            concerns.append(f"visual_{criterion}_{normalized}")
    return concerns


def propose_triage(
    assessments: list[Assessment],
    items: list[SelectionItem],
    brief: Brief,
) -> list[Decision]:
    """Propose keep/review/unassessed decisions; auto-reject is intentionally disabled."""

    assessment_ids = [assessment.asset_id for assessment in assessments]
    if len(set(assessment_ids)) != len(assessment_ids):
        raise ValueError("assessment asset IDs must be unique")
    by_asset = {assessment.asset_id: assessment for assessment in assessments}
    decisions: list[Decision] = []
    for item in items:
        missing = set(item.member_asset_ids) - set(by_asset)
        reasons: list[str] = []
        if missing:
            status = DecisionStatus.UNASSESSED
            reasons.append("technical_evidence_unavailable")
            member_assessments: list[Assessment] = []
        else:
            member_assessments = [by_asset[asset_id] for asset_id in item.member_asset_ids]
            reasons.extend(
                reason for assessment in member_assessments for reason in assessment.uncertainty
            )
            reasons.extend(
                concern for assessment in member_assessments for concern in _visual_concerns(assessment)
            )
            visual_statuses = {assessment.visual_status for assessment in member_assessments}
            if visual_statuses != {"evaluated"}:
                reasons.append("visual_evidence_incomplete")
            if all(
                assessment.evaluation_status == "unassessed"
                for assessment in member_assessments
            ):
                status = DecisionStatus.UNASSESSED
                reasons.append("technical_evidence_unavailable")
            elif reasons:
                status = DecisionStatus.REVIEW
            else:
                status = DecisionStatus.KEEP

        if item.item_id in brief.preselected_item_ids:
            reasons.append("unique_moment")
            if status != DecisionStatus.UNASSESSED:
                status = DecisionStatus.REVIEW

        decisions.append(
            Decision(
                item_id=item.item_id,
                status=status,
                tier=SelectionTier.NONE,
                rating_proposal=None,
                reason_codes=sorted(set(reasons)),
                human_reason=(
                    "Needs review because technical or visual evidence is incomplete."
                    if status in {DecisionStatus.REVIEW, DecisionStatus.UNASSESSED}
                    else "No technical concern was established by the available evidence."
                ),
                group_ids=list(item.group_ids),
                provenance={
                    "stage": "technical_triage",
                    "auto_reject_enabled": False,
                    "visual_statuses": sorted(
                        {assessment.visual_status for assessment in member_assessments}
                    ),
                },
                decision_authority=DecisionAuthority.PROPOSAL,
                user_override=False,
            )
        )
    return decisions


def _read_manifest(output: Path) -> Manifest:
    path = output / "assets.jsonl"
    if not path.is_file():
        raise FileNotFoundError(f"asset manifest does not exist: {path}")
    assets: list[Asset] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            assets.append(Asset.model_validate_json(line))
        except ValidationError as error:
            raise ValueError(f"invalid assets.jsonl line {line_number}: {error}") from error
    return Manifest(assets=assets)


def _read_json_array(path: Path, model: type[ModelT]) -> list[ModelT]:
    if not path.is_file():
        raise FileNotFoundError(f"required artifact does not exist: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"invalid {path.name}: {error}") from error
    if not isinstance(value, list):
        raise ValueError(f"invalid {path.name}: expected a JSON array")
    try:
        return [model.model_validate(item) for item in value]
    except ValidationError as error:
        raise ValueError(f"invalid {path.name}: {error}") from error


def _read_assessments(output: Path) -> list[Assessment]:
    path = output / "assessments.jsonl"
    if not path.is_file():
        raise FileNotFoundError(f"assessment artifact does not exist: {path}")
    assessments: list[Assessment] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            assessments.append(Assessment.model_validate_json(line))
        except ValidationError as error:
            raise ValueError(f"invalid assessments.jsonl line {line_number}: {error}") from error
    return assessments


def _jsonl(values: list[BaseModel]) -> str:
    return "".join(value.model_dump_json() + "\n" for value in values)


def _vision_capability(config: RunConfig) -> str:
    configured = config.editor_config_snapshot.get("vision_capability")
    value = configured if isinstance(configured, str) else os.environ.get(
        "SILVERLOUPE_VISION_CAPABILITY", "unavailable"
    )
    normalized = value.strip().casefold()
    return normalized if normalized in {"ready", "degraded", "unavailable"} else "unavailable"


def _assessment_report(
    assessments: list[Assessment], decisions: list[Decision], vision: str
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": (
            "completed"
            if assessments and all(value.visual_status == "evaluated" for value in assessments)
            else "partial"
        ),
        "assets": len(assessments),
        "assessment_statuses": dict(
            sorted(Counter(value.evaluation_status for value in assessments).items())
        ),
        "visual_statuses": dict(
            sorted(Counter(value.visual_status for value in assessments).items())
        ),
        "decision_statuses": dict(
            sorted(Counter(str(value.status) for value in decisions).items())
        ),
        "vision": vision,
        "auto_reject_enabled": False,
        "automatic_rejects": 0,
        "real_photo_validation": "unavailable",
    }


def _publish_assessment_artifacts(
    output: Path,
    assessments: list[Assessment],
    decisions: list[Decision],
    report: dict[str, Any],
) -> None:
    artifacts = {
        output / "assessments.jsonl": _jsonl(assessments),
        output / "decisions.jsonl": _jsonl(decisions),
        output / "assessment-report.json": json.dumps(
            report, ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n",
    }
    for path, contents in artifacts.items():
        atomic_write_text(path, contents)


def assess_prepared(output: Path) -> dict[str, Any]:
    output = output.expanduser().resolve()
    validate_output_layout(output)
    config = load_run_config(output)
    manifest = _read_manifest(output)
    groups = _read_json_array(output / "groups.json", Group)
    items = _read_json_array(output / "selection-items.json", SelectionItem)
    assessments = assess(manifest, groups, config.brief, config)
    decisions = propose_triage(assessments, items, config.brief)
    report = _assessment_report(assessments, decisions, _vision_capability(config))
    _publish_assessment_artifacts(output, assessments, decisions, report)
    return report


def create_vision_request_prepared(output: Path) -> VisionRequest:
    output = output.expanduser().resolve()
    validate_output_layout(output)
    config = load_run_config(output)
    request = build_vision_request(
        _read_manifest(output), _read_assessments(output), config.brief, config
    )
    atomic_write_text(
        output / "vision-request.json",
        request.model_dump_json(indent=2) + "\n",
    )
    atomic_write_text(
        output / "vision-status.json",
        json.dumps(
            {"schema_version": 1, "status": "pending", "attempt": request.attempt},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return request


def import_vision_response_prepared(output: Path, response_path: Path) -> VisionImportResult:
    output = output.expanduser().resolve()
    validate_output_layout(output)
    try:
        request = VisionRequest.model_validate_json(
            (output / "vision-request.json").read_text(encoding="utf-8")
        )
    except FileNotFoundError as error:
        raise FileNotFoundError(f"vision request does not exist: {output / 'vision-request.json'}") from error
    except (OSError, ValidationError) as error:
        raise ValueError(f"invalid vision-request.json: {error}") from error
    response = parse_vision_response(response_path.read_text(encoding="utf-8"))
    assessments = _read_assessments(output)
    result = apply_vision_response(request, response, assessments)

    if result.next_request is not None:
        atomic_write_text(
            output / "vision-request.json", result.next_request.model_dump_json(indent=2) + "\n"
        )
        atomic_write_text(
            output / "vision-status.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "retry_required",
                    "attempt": result.next_request.attempt,
                    "last_error": response.error,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        return result

    config = load_run_config(output)
    items = _read_json_array(output / "selection-items.json", SelectionItem)
    decisions = propose_triage(result.assessments, items, config.brief)
    report = _assessment_report(
        result.assessments,
        decisions,
        "ready" if result.status == "completed" else "degraded",
    )
    _publish_assessment_artifacts(output, result.assessments, decisions, report)
    atomic_write_text(
        output / "vision-status.json",
        json.dumps(
            {"schema_version": 1, "status": result.status, "attempt": request.attempt},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return result
