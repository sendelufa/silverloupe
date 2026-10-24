"""Versioned JSON contracts shared by the Silverloupe runtime stages."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = 1


def _duplicates(values: list[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


class ContractModel(BaseModel):
    """Base for strict, explicitly versioned exchange documents."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION


class RuntimeMode(StrEnum):
    LOCAL = "local"
    HOST_VISION = "host-vision"
    API = "api"


class DecisionStatus(StrEnum):
    KEEP = "keep"
    REJECT = "reject"
    REVIEW = "review"
    UNASSESSED = "unassessed"


class SelectionTier(StrEnum):
    TOP = "top"
    SELECT = "select"
    RESERVE = "reserve"
    NONE = "none"


class DecisionAuthority(StrEnum):
    PROPOSAL = "proposal"
    USER = "user"
    VALIDATED_POLICY = "validated_policy"


class Brief(ContractModel):
    genres: list[str] = Field(default_factory=list)
    purpose: str | None = None
    top_k: int | None = Field(default=None, ge=0)
    coverage_checklist: list[str] = Field(default_factory=list)
    repetition_limits: dict[str, int] = Field(default_factory=dict)
    allowed_artistic_techniques: list[str] = Field(default_factory=list)
    preselected_item_ids: list[str] = Field(default_factory=list)


class Asset(ContractModel):
    asset_id: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(min_length=1)
    exif: dict[str, Any] = Field(default_factory=dict)
    dimensions: tuple[int, int] | None = None
    decode_status: Literal["ready", "degraded", "unassessed", "unsupported", "error"] = "unassessed"
    existing_metadata: dict[str, Any] = Field(default_factory=dict)
    preview_provenance: dict[str, Any] | None = None

    @field_validator("relative_path")
    @classmethod
    def relative_path_stays_relative(cls, value: str) -> str:
        if Path(value).is_absolute():
            raise ValueError("relative_path must be relative to the session source root")
        if ".." in Path(value).parts:
            raise ValueError("relative_path must not escape the session source root")
        return value


class Manifest(ContractModel):
    assets: list[Asset]

    @model_validator(mode="after")
    def asset_ids_are_unique(self) -> Manifest:
        duplicates = _duplicates([asset.asset_id for asset in self.assets])
        if duplicates:
            raise ValueError(f"duplicate asset_id values: {sorted(duplicates)}")
        return self


class Group(ContractModel):
    group_id: str = Field(min_length=1)
    type: Literal[
        "byte_duplicate",
        "raw_jpeg_companion",
        "near_duplicate",
        "burst",
        "hdr",
        "panorama",
        "focus_stack",
        "single",
        "uncertain",
    ]
    member_ids: list[str] = Field(min_length=1)
    confidence_kind: Literal["measured", "heuristic", "manual", "unknown"] = "unknown"
    confidence_value: float | None = Field(default=None, ge=0, le=1)
    protected_set: bool = False
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class SelectionItem(ContractModel):
    item_id: str = Field(min_length=1)
    kind: Literal["capture", "composite"]
    member_asset_ids: list[str] = Field(min_length=1)
    primary_asset_id: str = Field(min_length=1)
    required_asset_ids: list[str] = Field(default_factory=list)
    group_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def members_are_consistent(self) -> SelectionItem:
        members = set(self.member_asset_ids)
        if len(members) != len(self.member_asset_ids):
            raise ValueError("member_asset_ids must be unique")
        if self.primary_asset_id not in members:
            raise ValueError("primary_asset_id must be present in member_asset_ids")
        missing_required = set(self.required_asset_ids) - members
        if missing_required:
            raise ValueError(f"required_asset_ids must be members: {sorted(missing_required)}")
        if self.kind == "composite" and not self.required_asset_ids:
            raise ValueError("composite items require required_asset_ids")
        return self


class Assessment(ContractModel):
    asset_id: str = Field(min_length=1)
    evaluation_status: Literal["assessed", "unassessed"] = "unassessed"
    technical_signals: dict[str, Any] = Field(default_factory=dict)
    visual_status: Literal["evaluated", "not_evaluated", "insufficient_evidence"] = "not_evaluated"
    visual_criteria: dict[str, Any] = Field(default_factory=dict)
    observed_artifacts: list[str] = Field(default_factory=list)
    region_evidence: list[dict[str, Any]] = Field(default_factory=list)
    intent_hypotheses: list[str] = Field(default_factory=list)
    uncertainty: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unassessed_has_no_proven_visual_result(self) -> Assessment:
        if self.evaluation_status == "unassessed" and (
            self.visual_status == "evaluated" or self.visual_criteria or self.region_evidence
        ):
            raise ValueError("unassessed assessment cannot claim a proven visual evaluation")
        return self


class Decision(ContractModel):
    item_id: str = Field(min_length=1)
    status: DecisionStatus
    tier: SelectionTier = SelectionTier.NONE
    rating_proposal: int | None = Field(default=None, ge=0, le=5)
    tags: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    human_reason: str | None = None
    group_ids: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    decision_authority: DecisionAuthority = DecisionAuthority.PROPOSAL
    user_override: bool = False


class Selection(ContractModel):
    selects_ids: list[str] = Field(default_factory=list)
    top_ids: list[str] = Field(default_factory=list)
    ranked_ids: list[str] = Field(default_factory=list)
    coverage: dict[str, Any] = Field(default_factory=dict)
    unmet_constraints: list[str] = Field(default_factory=list)
    protected_sets: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def selection_sets_are_consistent(self) -> Selection:
        for field_name in ("selects_ids", "top_ids", "ranked_ids"):
            values = getattr(self, field_name)
            if len(set(values)) != len(values):
                raise ValueError(f"{field_name} must contain unique item IDs")
        if not set(self.top_ids).issubset(self.selects_ids):
            raise ValueError("top_ids must be a subset of selects_ids")
        return self


class RunConfig(ContractModel):
    source_roots: list[str] = Field(min_length=1)
    output_root: str
    brief: Brief
    runtime_mode: RuntimeMode = RuntimeMode.HOST_VISION
    rendering_policy: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    editor_config_snapshot: dict[str, Any] = Field(default_factory=dict)


class RunState(ContractModel):
    session_id: str
    revision: int = Field(ge=1)
    status: Literal["completed", "partial", "failed"]
    output_root: str
    artifact_paths: dict[str, str] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def paths_are_output_owned(self) -> RunState:
        if not Path(self.output_root).is_absolute():
            raise ValueError("output_root must be absolute")
        for name, value in self.artifact_paths.items():
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"artifact path {name!r} must stay relative to output_root")
        return self


class SelectionOverride(ContractModel):
    item_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    selection_scope: Literal["top", "selects"]
    action: Literal["include", "exclude"]
    reason: str = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def reason_is_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized


class AssetMapping(ContractModel):
    asset_id: str
    target_id: str
    source_path: str | None = None
    metadata_path: str | None = None
    expected_before: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def mapping_paths_are_relative(self) -> AssetMapping:
        for name in ("source_path", "metadata_path"):
            value = getattr(self, name)
            if value is None:
                continue
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"{name} must stay relative to source_root")
        return self


class ProposedChange(ContractModel):
    asset_id: str
    item_id: str | None = None
    field: str
    value: Any


class DeliveryPlan(ContractModel):
    target: str
    profile_version: str
    plan_id: str = ""
    backend: Literal["xmp", "native", "unavailable"] = "unavailable"
    status: Literal["ready", "report_only"] = "report_only"
    source_root: str | None = None
    output_root: str | None = None
    journal_path: str | None = None
    accepted_reject_hash: str | None = None
    previous_receipt: dict[str, Any] = Field(default_factory=dict)
    asset_mappings: list[AssetMapping] = Field(default_factory=list)
    proposed_changes: list[ProposedChange] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)


class DeliveryOutcome(ContractModel):
    asset_id: str
    status: Literal["applied", "skipped", "failed"]
    operation_id: str | None = None
    target_id: str | None = None
    readback_values: dict[str, Any] = Field(default_factory=dict)
    conflict: dict[str, Any] | None = None
    error: str | None = None


class DeliveryReceipt(ContractModel):
    target: str
    receipt_id: str = ""
    plan_id: str = ""
    status: Literal["complete", "partial", "rolled_back"] = "complete"
    source_root: str | None = None
    output_root: str | None = None
    outcomes: list[DeliveryOutcome] = Field(default_factory=list)
    backups: list[str] = Field(default_factory=list)
    rollback_data: dict[str, Any] = Field(default_factory=dict)
    operation_ids: list[str] = Field(default_factory=list)
    field_ownership: dict[str, Any] = Field(default_factory=dict)


class InstallConfig(ContractModel):
    host: Literal["codex", "claude-code"]
    editor: Literal["lightroom", "photoshop-camera-raw", "lightcraft", "report-only"]
    metadata_backend: str | None = None
    install_raw_extra: bool = False
    runtime_mode: RuntimeMode = RuntimeMode.HOST_VISION


class SessionConfig(ContractModel):
    session_id: str
    source_roots: list[str]
    brief: Brief
    tool_versions: dict[str, str] = Field(default_factory=dict)
    model_versions: dict[str, str] = Field(default_factory=dict)
    profile_versions: dict[str, str] = Field(default_factory=dict)
    rendering_policy: dict[str, Any] = Field(default_factory=dict)
    runtime_mode: RuntimeMode = RuntimeMode.HOST_VISION
    budget: dict[str, Any] = Field(default_factory=dict)
    timestamps: dict[str, str] = Field(default_factory=dict)
    revision: int = Field(default=1, ge=1)
    editor_config_snapshot: dict[str, Any] = Field(default_factory=dict)
