"""Strict contracts for auditable host-vision requests and responses."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from silverloupe.contracts import Assessment, ContractModel


class VisionArtifact(ContractModel):
    artifact_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    kind: Literal["overview", "detail_crop"]
    path: str = Field(min_length=1)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class VisionRequestAsset(ContractModel):
    asset_id: str = Field(min_length=1)
    artifact_ids: list[str] = Field(min_length=1)
    criteria: list[str] = Field(min_length=1)


class VisionRequest(ContractModel):
    request_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    max_retries: int = Field(ge=0, le=2)
    artifacts: list[VisionArtifact] = Field(min_length=1)
    assets: list[VisionRequestAsset] = Field(min_length=1)
    instructions: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def references_are_unique_and_known(self) -> VisionRequest:
        artifact_ids = [artifact.artifact_id for artifact in self.artifacts]
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("vision artifact IDs must be unique")
        asset_ids = [asset.asset_id for asset in self.assets]
        if len(set(asset_ids)) != len(asset_ids):
            raise ValueError("vision request asset IDs must be unique")
        known_artifacts = set(artifact_ids)
        artifact_owners = {artifact.artifact_id: artifact.asset_id for artifact in self.artifacts}
        for asset in self.assets:
            unknown = set(asset.artifact_ids) - known_artifacts
            if unknown:
                raise ValueError(f"unknown request artifact IDs: {sorted(unknown)}")
            wrong_owner = [
                artifact_id
                for artifact_id in asset.artifact_ids
                if artifact_owners[artifact_id] != asset.asset_id
            ]
            if wrong_owner:
                raise ValueError(f"request artifacts belong to another asset: {sorted(wrong_owner)}")
        return self


class RegionEvidence(ContractModel):
    artifact_id: str = Field(min_length=1)
    region: tuple[float, float, float, float]
    finding: str = Field(min_length=1)

    @model_validator(mode="after")
    def region_is_normalized(self) -> RegionEvidence:
        x, y, width, height = self.region
        if min(x, y, width, height) < 0 or max(x, y, width, height) > 1:
            raise ValueError("region coordinates must be normalized to 0..1")
        if width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
            raise ValueError("region must have positive size and stay within the artifact")
        return self


class VisionObservation(ContractModel):
    asset_id: str = Field(min_length=1)
    observed_artifact_ids: list[str] = Field(min_length=1)
    visual_criteria: dict[str, str] = Field(default_factory=dict)
    region_evidence: list[RegionEvidence] = Field(default_factory=list)
    intent_hypotheses: list[str] = Field(default_factory=list)
    uncertainty: list[str] = Field(default_factory=list)


class VisionResponse(ContractModel):
    request_id: str = Field(min_length=1)
    status: Literal["completed", "partial", "transient_error"]
    observations: list[VisionObservation] = Field(default_factory=list)
    error: str | None = None

    @model_validator(mode="after")
    def status_matches_payload(self) -> VisionResponse:
        asset_ids = [observation.asset_id for observation in self.observations]
        if len(set(asset_ids)) != len(asset_ids):
            raise ValueError("vision response asset IDs must be unique")
        if self.status == "transient_error":
            if self.observations:
                raise ValueError("transient_error response cannot include observations")
            if not self.error:
                raise ValueError("transient_error response requires an error")
        elif self.error:
            raise ValueError("completed or partial response cannot include an error")
        return self


class VisionImportResult(ContractModel):
    status: Literal["completed", "partial", "retry_required"]
    assessments: list[Assessment]
    next_request: VisionRequest | None = None
