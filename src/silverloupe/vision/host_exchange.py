"""Build and validate file-based exchanges with host-provided vision."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from silverloupe.contracts import Assessment, Brief, Manifest, RunConfig
from silverloupe.vision.base import (
    VisionArtifact,
    VisionImportResult,
    VisionRequest,
    VisionRequestAsset,
    VisionResponse,
)


_CRITERIA = [
    "focus_on_relevant_subject",
    "motion_blur_and_intent",
    "significant_region_exposure",
    "eye_state_when_contextually_relevant",
    "accidental_subject_occlusion",
    "artistic_intent_hypotheses",
]


def _vision_policy(config: RunConfig) -> dict[str, int]:
    raw = config.budget.get("vision_policy", {})
    if not isinstance(raw, dict):
        raise ValueError("vision_policy must be an object")
    max_retries = raw.get("max_retries", 2)
    if isinstance(max_retries, bool) or not isinstance(max_retries, int):
        raise ValueError("vision_policy.max_retries must be an integer between 0 and 2")
    if not 0 <= max_retries <= 2:
        raise ValueError("vision_policy.max_retries must be between 0 and 2")
    return {"max_retries": max_retries}


def _safe_preview(asset_id: str, relative_value: object, output: Path) -> tuple[Path, str] | None:
    if not isinstance(relative_value, str):
        return None
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    candidate = output / relative
    if candidate.is_symlink() or not candidate.resolve().is_relative_to(output):
        return None
    if not candidate.is_file():
        return None
    return candidate, relative.as_posix()


def _artifact_id(asset_id: str, relative_path: str, digest: str) -> str:
    payload = f"{asset_id}\0{relative_path}\0{digest}".encode("utf-8")
    return f"overview-{hashlib.sha256(payload).hexdigest()[:20]}"


def build_vision_request(
    manifest: Manifest,
    assessments: list[Assessment],
    brief: Brief,
    config: RunConfig,
    *,
    attempt: int = 1,
) -> VisionRequest:
    """Create a deterministic request containing only safe, existing overview artifacts."""

    policy = _vision_policy(config)
    if attempt < 1 or attempt > policy["max_retries"] + 1:
        raise ValueError("vision request attempt exceeds the configured retry budget")

    assets_by_id = {asset.asset_id: asset for asset in manifest.assets}
    assessment_ids = [assessment.asset_id for assessment in assessments]
    if len(set(assessment_ids)) != len(assessment_ids):
        raise ValueError("assessment asset IDs must be unique")
    unknown = set(assessment_ids) - set(assets_by_id)
    if unknown:
        raise ValueError(f"assessments reference unknown assets: {sorted(unknown)}")

    output = Path(config.output_root).expanduser().resolve()
    artifacts: list[VisionArtifact] = []
    request_assets: list[VisionRequestAsset] = []
    for assessment in sorted(assessments, key=lambda value: value.asset_id):
        if assessment.evaluation_status != "assessed" or assessment.visual_status == "evaluated":
            continue
        asset = assets_by_id[assessment.asset_id]
        safe = _safe_preview(
            asset.asset_id, (asset.preview_provenance or {}).get("path"), output
        )
        if safe is None:
            continue
        path, relative_path = safe
        try:
            with Image.open(path) as opened:
                width, height = opened.size
                opened.verify()
        except (Image.DecompressionBombError, OSError, UnidentifiedImageError):
            continue
        artifact = VisionArtifact(
            artifact_id=_artifact_id(asset.asset_id, relative_path, asset.content_sha256),
            asset_id=asset.asset_id,
            kind="overview",
            path=relative_path,
            width=width,
            height=height,
        )
        artifacts.append(artifact)
        request_assets.append(
            VisionRequestAsset(
                asset_id=asset.asset_id,
                artifact_ids=[artifact.artifact_id],
                criteria=list(_CRITERIA),
            )
        )

    if not artifacts:
        raise ValueError("no safe preview artifacts are available for host vision")

    identity = {
        "artifacts": [artifact.model_dump(mode="json") for artifact in artifacts],
        "assets": [asset.model_dump(mode="json") for asset in request_assets],
        "brief": brief.model_dump(mode="json"),
    }
    request_id = "vision-" + hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:20]
    return VisionRequest(
        request_id=request_id,
        attempt=attempt,
        max_retries=policy["max_retries"],
        artifacts=artifacts,
        assets=request_assets,
        instructions=[
            "Report only properties visible in an artifact listed for the same asset.",
            "Reference every inspected artifact by artifact_id; never infer a visual result from metadata alone.",
            "Treat blur, low/high key, grain, silhouettes, and closed eyes as context-dependent, not automatic failure.",
            "Use uncertainty when overview resolution is insufficient; request a future detail crop rather than guessing.",
        ],
    )


def parse_vision_response(text: str) -> VisionResponse:
    """Parse strict response JSON while preserving a clear invalid-input boundary."""

    try:
        value = json.loads(text)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid vision response JSON: {error}") from error
    try:
        return VisionResponse.model_validate(value)
    except ValidationError as error:
        raise ValueError(f"invalid vision response: {error}") from error


def _mark_missing_evidence(assessment: Assessment, reason: str) -> Assessment:
    return assessment.model_copy(
        update={
            "visual_status": "insufficient_evidence",
            "uncertainty": sorted(set([*assessment.uncertainty, reason])),
        }
    )


def apply_vision_response(
    request: VisionRequest,
    response: VisionResponse,
    assessments: list[Assessment],
) -> VisionImportResult:
    """Validate provenance and merge host observations without inventing visual success."""

    if response.request_id != request.request_id:
        raise ValueError("vision response request_id does not match the active request")

    requested_assets = {asset.asset_id: asset for asset in request.assets}
    assessments_by_id = {assessment.asset_id: assessment for assessment in assessments}
    if len(assessments_by_id) != len(assessments):
        raise ValueError("assessment asset IDs must be unique")
    missing_assessments = set(requested_assets) - set(assessments_by_id)
    if missing_assessments:
        raise ValueError(f"vision request assets lack assessments: {sorted(missing_assessments)}")

    if response.status == "transient_error":
        if request.attempt <= request.max_retries:
            return VisionImportResult(
                status="retry_required",
                assessments=assessments,
                next_request=request.model_copy(update={"attempt": request.attempt + 1}),
            )
        exhausted = [
            _mark_missing_evidence(
                assessment, "vision_unavailable_after_retries"
            )
            if assessment.asset_id in requested_assets
            else assessment
            for assessment in assessments
        ]
        return VisionImportResult(status="partial", assessments=exhausted)

    observations = {observation.asset_id: observation for observation in response.observations}
    unknown_assets = set(observations) - set(requested_assets)
    if unknown_assets:
        raise ValueError(f"vision response references unrequested assets: {sorted(unknown_assets)}")
    missing = set(requested_assets) - set(observations)
    if response.status == "completed" and missing:
        raise ValueError(f"completed vision response is missing observations: {sorted(missing)}")

    updated = dict(assessments_by_id)
    for asset_id, observation in observations.items():
        allowed_artifacts = set(requested_assets[asset_id].artifact_ids)
        unknown_artifacts = set(observation.observed_artifact_ids) - allowed_artifacts
        if unknown_artifacts:
            raise ValueError(
                f"unknown observed artifact IDs for {asset_id!r}: {sorted(unknown_artifacts)}"
            )
        observed = set(observation.observed_artifact_ids)
        invalid_regions = [
            evidence.artifact_id
            for evidence in observation.region_evidence
            if evidence.artifact_id not in observed
        ]
        if invalid_regions:
            raise ValueError(
                f"region evidence must reference an observed artifact: {sorted(invalid_regions)}"
            )
        current = assessments_by_id[asset_id]
        if current.evaluation_status != "assessed":
            raise ValueError(f"unassessed asset {asset_id!r} cannot receive proven visual evidence")
        updated[asset_id] = current.model_copy(
            update={
                "visual_status": "evaluated",
                "visual_criteria": dict(observation.visual_criteria),
                "observed_artifacts": list(observation.observed_artifact_ids),
                "region_evidence": [
                    evidence.model_dump(mode="json") for evidence in observation.region_evidence
                ],
                "intent_hypotheses": list(observation.intent_hypotheses),
                "uncertainty": sorted(
                    set([*current.uncertainty, *observation.uncertainty])
                ),
            }
        )
    for asset_id in missing:
        updated[asset_id] = _mark_missing_evidence(
            assessments_by_id[asset_id], "vision_response_missing"
        )

    status = "completed" if response.status == "completed" and not missing else "partial"
    return VisionImportResult(
        status=status,
        assessments=[updated[assessment.asset_id] for assessment in assessments],
    )
