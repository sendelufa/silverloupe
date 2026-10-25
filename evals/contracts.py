"""Development-only contracts for the Silverloupe evaluation harness."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from silverloupe.contracts import ContractModel


class TrialResult(ContractModel):
    case_id: str
    host: str
    variant: str
    output_dir: str
    status: Literal["passed", "failed", "partial"]
    transcript_path: str | None = None
    tool_trace_path: str | None = None
    wall_time_seconds: float = Field(ge=0)
    tokens: dict[str, int] = Field(default_factory=dict)
    cost: dict[str, float] = Field(default_factory=dict)
    versions: dict[str, str] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class HostObservation(ContractModel):
    """Structured, observable result requested from each evaluated host."""

    status: Literal["passed", "failed", "partial"]
    selected_skill: str | None = None
    disclosures: list[str] = Field(default_factory=list)
    reference_reads: list[str] = Field(default_factory=list)
    artifact_paths: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
