"""Explainable, recommendation-only technical assessment and triage."""

from __future__ import annotations

import hashlib
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
    validate_vision_request_inputs,
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
    _validate_assessment_generation(output)
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


def _read_decisions(output: Path) -> list[Decision]:
    _validate_assessment_generation(output)
    path = output / "decisions.jsonl"
    if not path.is_file():
        raise FileNotFoundError(f"decision artifact does not exist: {path}")
    decisions: list[Decision] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            decisions.append(Decision.model_validate_json(line))
        except ValidationError as error:
            raise ValueError(f"invalid decisions.jsonl line {line_number}: {error}") from error
    return decisions


def _jsonl(values: list[BaseModel]) -> str:
    return "".join(value.model_dump_json() + "\n" for value in values)


def _generation_status(output: Path) -> str | None:
    marker = output / "assessment-generation.json"
    if not marker.is_file():
        return None
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"invalid assessment-generation.json: {error}") from error
    if not isinstance(value, dict) or value.get("status") not in {"publishing", "complete"}:
        raise ValueError("invalid assessment-generation.json: unknown status")
    return str(value["status"])


def _validate_assessment_generation(output: Path) -> None:
    status = _generation_status(output)
    if status != "complete":
        detail = "is incomplete" if status == "publishing" else "marker is missing"
        raise ValueError(f"assessment generation {detail}; run assess again")


def _preserve_visual_evidence(
    current: list[Assessment], previous: list[Assessment]
) -> tuple[list[Assessment], bool]:
    previous_by_id = {assessment.asset_id: assessment for assessment in previous}
    preserved: list[Assessment] = []
    invalidated = False
    for assessment in current:
        prior = previous_by_id.get(assessment.asset_id)
        current_fingerprint = assessment.technical_signals.get("input_fingerprint")
        prior_fingerprint = (
            prior.technical_signals.get("input_fingerprint") if prior is not None else None
        )
        if (
            prior is not None
            and assessment.evaluation_status == "assessed"
            and prior.visual_status in {"evaluated", "insufficient_evidence"}
            and isinstance(current_fingerprint, str)
            and current_fingerprint == prior_fingerprint
        ):
            preserved.append(
                assessment.model_copy(
                    update={
                        "visual_status": prior.visual_status,
                        "visual_criteria": dict(prior.visual_criteria),
                        "observed_artifacts": list(prior.observed_artifacts),
                        "region_evidence": list(prior.region_evidence),
                        "intent_hypotheses": list(prior.intent_hypotheses),
                        "uncertainty": sorted(
                            set([*assessment.uncertainty, *prior.uncertainty])
                        ),
                    }
                )
            )
        else:
            preserved.append(assessment)
            invalidated = invalidated or bool(
                prior is not None
                and prior.visual_status in {"evaluated", "insufficient_evidence"}
            )
    return preserved, invalidated


def _preserve_user_decisions(
    proposals: list[Decision], previous: list[Decision]
) -> list[Decision]:
    user_by_id = {
        decision.item_id: decision
        for decision in previous
        if decision.decision_authority == DecisionAuthority.USER or decision.user_override
    }
    return [user_by_id.get(proposal.item_id, proposal) for proposal in proposals]


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
    curation_marker = output / "curation-generation.json"
    if curation_marker.is_file():
        previous: dict[str, Any] = {}
        try:
            loaded = json.loads(curation_marker.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                previous = loaded
        except (OSError, ValueError):
            pass
        previous.update(
            {
                "schema_version": 1,
                "status": "invalidated",
                "reason": "assessment_changed",
            }
        )
        atomic_write_text(
            curation_marker,
            json.dumps(previous, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
        )
    artifacts = {
        output / "assessments.jsonl": _jsonl(assessments),
        output / "decisions.jsonl": _jsonl(decisions),
        output / "assessment-report.json": json.dumps(
            report, ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n",
    }
    generation_id = hashlib.sha256(
        "".join(f"{path.name}\0{contents}" for path, contents in artifacts.items()).encode(
            "utf-8"
        )
    ).hexdigest()
    marker_path = output / "assessment-generation.json"
    marker = {
        "schema_version": 1,
        "generation_id": generation_id,
        "status": "publishing",
        "files": [path.name for path in artifacts],
    }
    atomic_write_text(
        marker_path,
        json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    for path, contents in artifacts.items():
        atomic_write_text(path, contents)
    marker["status"] = "complete"
    atomic_write_text(
        marker_path,
        json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def assess_prepared(output: Path) -> dict[str, Any]:
    output = output.expanduser().resolve()
    validate_output_layout(output)
    config = load_run_config(output)
    manifest = _read_manifest(output)
    groups = _read_json_array(output / "groups.json", Group)
    items = _read_json_array(output / "selection-items.json", SelectionItem)
    assessments = assess(manifest, groups, config.brief, config)
    previous_assessments: list[Assessment] = []
    previous_decisions: list[Decision] = []
    if _generation_status(output) == "complete":
        previous_assessments = _read_assessments(output)
        previous_decisions = _read_decisions(output)
    assessments, invalidated = _preserve_visual_evidence(
        assessments, previous_assessments
    )
    decisions = propose_triage(assessments, items, config.brief)
    decisions = _preserve_user_decisions(decisions, previous_decisions)
    report = _assessment_report(assessments, decisions, _vision_capability(config))
    _publish_assessment_artifacts(output, assessments, decisions, report)
    if invalidated and (output / "vision-status.json").is_file():
        atomic_write_text(
            output / "vision-status.json",
            json.dumps(
                {"schema_version": 1, "status": "invalidated", "reason": "inputs_changed"},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
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
    config = load_run_config(output)
    manifest = _read_manifest(output)
    validate_vision_request_inputs(
        request, manifest, assessments, config.brief, config
    )
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

    items = _read_json_array(output / "selection-items.json", SelectionItem)
    decisions = propose_triage(result.assessments, items, config.brief)
    decisions = _preserve_user_decisions(decisions, _read_decisions(output))
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
