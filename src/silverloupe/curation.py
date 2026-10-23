"""Deterministic, explainable genre-aware curation."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from silverloupe.contracts import Assessment, Brief, Group, Manifest, SelectionItem


_COMPETITIVE_GROUP_TYPES = {"burst", "near_duplicate"}
_KNOWN_GENRES = {"wedding", "architecture", "landscape"}
_CATEGORY_SCORES = {
    "acceptable": 1.0,
    "concern": -1.0,
    "uncertain": 0.0,
    "not_applicable": 0.0,
    "insufficient_evidence": -0.25,
}
_GENERIC_WEIGHTS = {
    "moment": 1.0,
    "expression": 1.0,
    "subject": 1.0,
    "composition": 1.0,
    "light": 1.0,
    "artistic_intent": 0.75,
    "technical_usability": 1.0,
    "repair_cost": -0.5,
    "focus_on_relevant_subject": 0.75,
    "motion_blur_and_intent": 0.5,
    "significant_region_exposure": 0.5,
    "accidental_subject_occlusion": 0.5,
}


class _GenreProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    profile_version: str = Field(min_length=1)
    genre: str = Field(min_length=1)
    criteria_weights: dict[str, float] = Field(min_length=1)
    diversity_penalty: float = Field(ge=0)
    artistic_exceptions: list[str] = Field(min_length=1)
    negative_examples: list[str] = Field(min_length=1)

    @field_validator("criteria_weights")
    @classmethod
    def weights_are_finite(cls, value: dict[str, float]) -> dict[str, float]:
        if not all(math.isfinite(weight) for weight in value.values()):
            raise ValueError("criteria weights must be finite")
        return value


@dataclass(frozen=True)
class ItemScore:
    """Uncalibrated comparative score and its auditable components."""

    item_id: str
    score: float
    components: dict[str, float]
    winner_id: str
    alternative_ids: list[str]
    profile_versions: dict[str, str]


def _resource_root() -> Path:
    return Path(__file__).resolve().parents[2] / "resources"


def _load_profiles(brief: Brief) -> list[_GenreProfile]:
    profiles: list[_GenreProfile] = []
    for genre in dict.fromkeys(value.strip().casefold() for value in brief.genres):
        if genre not in _KNOWN_GENRES:
            continue
        path = _resource_root() / "profiles" / f"{genre}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            profile = _GenreProfile.model_validate(value)
        except FileNotFoundError as error:
            raise FileNotFoundError(f"genre profile does not exist: {path}") from error
        except (OSError, ValueError, ValidationError) as error:
            raise ValueError(f"invalid genre profile {genre!r}: {error}") from error
        if profile.genre != genre:
            raise ValueError(
                f"invalid genre profile {genre!r}: profile genre is {profile.genre!r}"
            )
        profiles.append(profile)
    return profiles


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)


def _validate_inputs(
    manifest: Manifest,
    groups: list[Group],
    items: list[SelectionItem],
    assessments: list[Assessment],
) -> None:
    asset_ids = {asset.asset_id for asset in manifest.assets}
    duplicate_group_ids = _duplicates([group.group_id for group in groups])
    if duplicate_group_ids:
        raise ValueError(f"group IDs must be unique: {duplicate_group_ids}")
    duplicate_item_ids = _duplicates([item.item_id for item in items])
    if duplicate_item_ids:
        raise ValueError(f"item IDs must be unique: {duplicate_item_ids}")
    duplicate_assessment_ids = _duplicates(
        [assessment.asset_id for assessment in assessments]
    )
    if duplicate_assessment_ids:
        raise ValueError(
            f"assessment asset IDs must be unique: {duplicate_assessment_ids}"
        )

    unknown_group_assets = sorted(
        {
            asset_id
            for group in groups
            for asset_id in group.member_ids
            if asset_id not in asset_ids
        }
    )
    if unknown_group_assets:
        raise ValueError(f"groups reference unknown assets: {unknown_group_assets}")
    unknown_item_assets = sorted(
        {
            asset_id
            for item in items
            for asset_id in item.member_asset_ids
            if asset_id not in asset_ids
        }
    )
    if unknown_item_assets:
        raise ValueError(
            f"selection items reference unknown assets: {unknown_item_assets}"
        )
    unknown_assessment_assets = sorted(
        assessment.asset_id
        for assessment in assessments
        if assessment.asset_id not in asset_ids
    )
    if unknown_assessment_assets:
        raise ValueError(
            f"assessments reference unknown assets: {unknown_assessment_assets}"
        )


def _criterion_value(value: Any) -> float | None:
    candidate: Any = value
    if isinstance(value, dict):
        candidate = value.get("score")
    if isinstance(candidate, bool):
        return None
    if isinstance(candidate, (int, float)):
        normalized = float(candidate)
        return normalized if math.isfinite(normalized) else None
    if isinstance(candidate, str):
        return _CATEGORY_SCORES.get(candidate.strip().casefold())
    return None


def _technical_readiness(assessment: Assessment) -> float:
    if assessment.evaluation_status == "unassessed":
        return -0.5
    if assessment.visual_status == "evaluated":
        return 0.5
    if assessment.visual_status == "insufficient_evidence":
        return -0.25
    return 0.0


def _weights(profiles: list[_GenreProfile]) -> dict[str, float]:
    result = dict(_GENERIC_WEIGHTS)
    if not profiles:
        return result
    genre_weights: dict[str, list[float]] = defaultdict(list)
    for profile in profiles:
        for criterion, weight in profile.criteria_weights.items():
            genre_weights[criterion].append(weight)
    for criterion, values in genre_weights.items():
        result[criterion] = sum(values) / len(values)
    return result


def _raw_item_scores(
    items: list[SelectionItem],
    assessments: list[Assessment],
    weights: dict[str, float],
) -> dict[str, tuple[float, dict[str, float]]]:
    by_asset = {assessment.asset_id: assessment for assessment in assessments}
    result: dict[str, tuple[float, dict[str, float]]] = {}
    for item in items:
        members = [
            by_asset[asset_id]
            for asset_id in item.member_asset_ids
            if asset_id in by_asset
        ]
        values_by_criterion: dict[str, list[float]] = defaultdict(list)
        readiness: list[float] = []
        for assessment in members:
            readiness.append(_technical_readiness(assessment))
            for criterion, value in assessment.visual_criteria.items():
                if criterion not in weights:
                    continue
                normalized = _criterion_value(value)
                if normalized is not None:
                    values_by_criterion[criterion].append(normalized)

        components = {
            criterion: sum(values) / len(values)
            for criterion, values in sorted(values_by_criterion.items())
        }
        components["technical_readiness"] = (
            sum(readiness) / len(readiness) if readiness else -0.5
        )
        score = components["technical_readiness"] + sum(
            weights[criterion] * value
            for criterion, value in components.items()
            if criterion in weights
        )
        result[item.item_id] = (score, components)
    return result


def _competitors(
    groups: list[Group], items: list[SelectionItem]
) -> dict[str, set[str]]:
    item_members = {item.item_id: set(item.member_asset_ids) for item in items}
    result: dict[str, set[str]] = defaultdict(set)
    for group in groups:
        if group.type not in _COMPETITIVE_GROUP_TYPES:
            continue
        members = set(group.member_ids)
        candidate_ids = sorted(
            item_id
            for item_id, item_asset_ids in item_members.items()
            if members & item_asset_ids
        )
        if len(candidate_ids) < 2:
            continue
        for item_id in candidate_ids:
            result[item_id].update(candidate_ids)
    return result


def score_items(
    manifest: Manifest,
    groups: list[Group],
    items: list[SelectionItem],
    assessments: list[Assessment],
    brief: Brief,
) -> list[ItemScore]:
    """Return stable, uncalibrated comparative item scores."""

    _validate_inputs(manifest, groups, items, assessments)
    profiles = _load_profiles(brief)
    profile_versions = {
        profile.genre: profile.profile_version for profile in profiles
    }
    raw_scores = _raw_item_scores(items, assessments, _weights(profiles))
    competitors = _competitors(groups, items)
    scores: list[ItemScore] = []
    for item in items:
        score, components = raw_scores[item.item_id]
        candidate_ids = competitors.get(item.item_id, {item.item_id})
        ranked_candidates = sorted(
            candidate_ids,
            key=lambda item_id: (-raw_scores[item_id][0], item_id),
        )
        scores.append(
            ItemScore(
                item_id=item.item_id,
                score=score,
                components=components,
                winner_id=ranked_candidates[0],
                alternative_ids=sorted(candidate_ids - {item.item_id}),
                profile_versions=dict(profile_versions),
            )
        )
    return sorted(scores, key=lambda value: (-value.score, value.item_id))
