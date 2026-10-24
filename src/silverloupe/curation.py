"""Deterministic, explainable genre-aware curation."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from silverloupe.contracts import (
    Assessment,
    Asset,
    Brief,
    Decision,
    DecisionAuthority,
    DecisionStatus,
    Group,
    Manifest,
    Selection,
    SelectionItem,
    SelectionTier,
)
from silverloupe.previews import validate_output_layout
from silverloupe.state import atomic_write_text, invalidate_report_generation, load_run_config


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
_GENERIC_EDITORIAL_CRITERIA = (
    "subject",
    "composition",
    "light",
    "artistic_intent",
    "technical_usability",
)
_NUMERIC_ONLY_CRITERIA = {"repair_cost"}
ModelT = TypeVar("ModelT", bound=BaseModel)


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
    diversity_penalty: float


def _load_profiles(brief: Brief) -> list[_GenreProfile]:
    profiles: list[_GenreProfile] = []
    for genre in dict.fromkeys(value.strip().casefold() for value in brief.genres):
        if genre not in _KNOWN_GENRES:
            continue
        path = files("silverloupe.resources").joinpath(
            "profiles", f"{genre}.json"
        )
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


def requested_editorial_criteria(brief: Brief) -> list[str]:
    """Return categorical editorial evidence keys for the host-vision exchange."""

    criteria = list(_GENERIC_EDITORIAL_CRITERIA)
    for profile in _load_profiles(brief):
        criteria.extend(
            criterion
            for criterion in profile.criteria_weights
            if criterion not in _NUMERIC_ONLY_CRITERIA
        )
    criteria.extend(
        f"coverage:{label.strip().casefold()}"
        for label in brief.coverage_checklist
        if label.strip()
    )
    return list(dict.fromkeys(criteria))


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
    known_group_ids = {group.group_id for group in groups}
    unknown_item_groups = sorted(
        {
            group_id
            for item in items
            for group_id in item.group_ids
            if group_id not in known_group_ids
        }
    )
    if unknown_item_groups:
        raise ValueError(
            f"selection items reference unknown groups: {unknown_item_groups}"
        )
    item_owners: dict[str, list[str]] = defaultdict(list)
    for item in items:
        for asset_id in item.member_asset_ids:
            item_owners[asset_id].append(item.item_id)
    overlapping = {
        asset_id: sorted(owner_ids)
        for asset_id, owner_ids in item_owners.items()
        if len(owner_ids) > 1
    }
    if overlapping:
        raise ValueError(f"selection items overlap on assets: {overlapping}")
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
        try:
            normalized = float(candidate)
        except (OverflowError, TypeError, ValueError):
            return None
        return (
            normalized
            if math.isfinite(normalized) and -1.0 <= normalized <= 1.0
            else None
        )
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
            criterion: math.fsum(values) / len(values)
            for criterion, values in sorted(values_by_criterion.items())
        }
        components["technical_readiness"] = (
            sum(readiness) / len(readiness) if readiness else -0.5
        )
        score = components["technical_readiness"] + math.fsum(
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
    diversity_penalty = (
        sum(profile.diversity_penalty for profile in profiles) / len(profiles)
        if profiles
        else 1.0
    )
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
                diversity_penalty=diversity_penalty,
            )
        )
    return sorted(scores, key=lambda value: (-value.score, value.item_id))


def _coverage_labels(
    item: SelectionItem, assessments_by_asset: dict[str, Assessment]
) -> set[str]:
    labels: set[str] = set()
    for asset_id in item.member_asset_ids:
        assessment = assessments_by_asset.get(asset_id)
        if assessment is None:
            continue
        for key in ("coverage", "coverage_labels"):
            value = assessment.visual_criteria.get(key)
            if isinstance(value, str):
                candidates = [value]
            elif isinstance(value, (list, tuple, set)):
                candidates = [candidate for candidate in value if isinstance(candidate, str)]
            else:
                candidates = []
            labels.update(
                candidate.strip().casefold()
                for candidate in candidates
                if candidate.strip()
            )
        labels.update(
            key.removeprefix("coverage:").strip().casefold()
            for key, value in assessment.visual_criteria.items()
            if key.startswith("coverage:")
            and isinstance(value, str)
            and value.strip().casefold() == "acceptable"
            and key.removeprefix("coverage:").strip()
        )
    return labels


def _competitive_groups_by_item(
    groups: list[Group], items: list[SelectionItem]
) -> dict[str, set[str]]:
    item_members = {item.item_id: set(item.member_asset_ids) for item in items}
    result: dict[str, set[str]] = defaultdict(set)
    for group in groups:
        if group.type not in _COMPETITIVE_GROUP_TYPES:
            continue
        members = set(group.member_ids)
        for item_id, asset_ids in item_members.items():
            if members & asset_ids:
                result[item_id].add(group.group_id)
    return result


def _groups_by_item(
    groups: list[Group], items: list[SelectionItem]
) -> dict[str, set[str]]:
    item_members = {item.item_id: set(item.member_asset_ids) for item in items}
    result: dict[str, set[str]] = defaultdict(set)
    for group in groups:
        members = set(group.member_ids)
        for item_id, asset_ids in item_members.items():
            if members & asset_ids:
                result[item_id].add(group.group_id)
    return result


def _validate_repetition_limits(brief: Brief, groups: list[Group]) -> None:
    known = {group.group_id for group in groups}
    unknown = sorted(set(brief.repetition_limits) - known)
    if unknown:
        raise ValueError(f"unknown repetition-limit groups: {unknown}")
    invalid = sorted(
        group_id
        for group_id, limit in brief.repetition_limits.items()
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
    )
    if invalid:
        raise ValueError(
            f"repetition limits must be non-negative integers: {invalid}"
        )


def _choose_top(
    eligible_scores: list[ItemScore],
    k: int,
    required_labels: list[str],
    coverage_by_item: dict[str, set[str]],
    competitive_groups: dict[str, set[str]],
    groups_by_item: dict[str, set[str]],
    initial_group_counts: dict[str, int],
    repetition_limits: dict[str, int],
) -> list[str]:
    remaining = {value.item_id: value for value in eligible_scores}
    selected: list[str] = []
    uncovered = set(required_labels)
    selected_groups = {
        group_id for group_id, count in initial_group_counts.items() if count > 0
    }
    group_counts = defaultdict(int, initial_group_counts)

    def allowed(item_id: str, counts: dict[str, int]) -> bool:
        return all(
            counts.get(group_id, 0) < repetition_limits[group_id]
            for group_id in groups_by_item.get(item_id, set())
            if group_id in repetition_limits
        )

    while remaining and len(selected) < k:
        covering = [
            value
            for value in remaining.values()
            if coverage_by_item[value.item_id] & uncovered
            and allowed(value.item_id, group_counts)
        ]
        if covering:
            chosen = min(
                covering,
                key=lambda value: (
                    -len(coverage_by_item[value.item_id] & uncovered),
                    -value.score,
                    value.item_id,
                ),
            )
        else:
            allowed_values = [
                value
                for value in remaining.values()
                if allowed(value.item_id, group_counts)
            ]
            if not allowed_values:
                break
            chosen = min(
                allowed_values,
                key=lambda value: (
                    -(
                        value.score
                        - value.diversity_penalty
                        * len(
                            competitive_groups.get(value.item_id, set())
                            & selected_groups
                        )
                    ),
                    value.item_id,
                ),
            )
        selected.append(chosen.item_id)
        uncovered -= coverage_by_item[chosen.item_id]
        selected_groups.update(competitive_groups.get(chosen.item_id, set()))
        for group_id in groups_by_item.get(chosen.item_id, set()):
            group_counts[group_id] += 1
        del remaining[chosen.item_id]

    score_by_id = {value.item_id: value.score for value in eligible_scores}
    required = set(required_labels)
    while selected:
        current_coverage = {
            label
            for item_id in selected
            for label in coverage_by_item[item_id]
            if label in required
        }
        best: tuple[int, float, tuple[str, ...], list[str]] | None = None
        selected_set = set(selected)
        for removed_id in selected:
            for added_id in score_by_id.keys() - selected_set:
                replacement = [
                    added_id if item_id == removed_id else item_id
                    for item_id in selected
                ]
                counts = defaultdict(int, initial_group_counts)
                for item_id in replacement:
                    for group_id in groups_by_item.get(item_id, set()):
                        counts[group_id] += 1
                if any(
                    counts[group_id] > limit
                    for group_id, limit in repetition_limits.items()
                ):
                    continue
                covered = {
                    label
                    for item_id in replacement
                    for label in coverage_by_item[item_id]
                    if label in required
                }
                if len(covered) <= len(current_coverage):
                    continue
                candidate = (
                    len(covered),
                    math.fsum(score_by_id[item_id] for item_id in replacement),
                    tuple(sorted(replacement)),
                    replacement,
                )
                if best is None or candidate[:2] > best[:2] or (
                    candidate[:2] == best[:2] and candidate[2] < best[2]
                ):
                    best = candidate
        if best is None:
            break
        selected = best[3]
    selected_set = set(selected)
    return [value.item_id for value in eligible_scores if value.item_id in selected_set]


def _item_status(
    item: SelectionItem,
    assessments_by_asset: dict[str, Assessment],
    brief: Brief,
) -> tuple[DecisionStatus, list[str]]:
    member_assessments = [
        assessments_by_asset[asset_id]
        for asset_id in item.member_asset_ids
        if asset_id in assessments_by_asset
    ]
    if not member_assessments or all(
        assessment.evaluation_status == "unassessed"
        for assessment in member_assessments
    ):
        status = DecisionStatus.UNASSESSED
        reasons = ["technical_evidence_unavailable"]
    else:
        reasons = [
            reason
            for assessment in member_assessments
            for reason in assessment.uncertainty
        ]
        reasons.extend(
            f"visual_{criterion}_{normalized}"
            for assessment in member_assessments
            for criterion, value in assessment.visual_criteria.items()
            if (normalized := str(value).strip().casefold())
            in {"concern", "uncertain", "insufficient_evidence"}
        )
        if len(member_assessments) != len(item.member_asset_ids):
            reasons.append("technical_evidence_unavailable")
        if any(
            assessment.visual_status != "evaluated"
            for assessment in member_assessments
        ):
            reasons.append("visual_evidence_incomplete")
        status = DecisionStatus.REVIEW if reasons else DecisionStatus.KEEP
    if item.item_id in brief.preselected_item_ids:
        reasons.append("unique_moment")
        if status != DecisionStatus.UNASSESSED:
            status = DecisionStatus.REVIEW
    return status, sorted(set(reasons))


def _generated_decision(
    item: SelectionItem,
    item_score: ItemScore,
    status: DecisionStatus,
    tier: SelectionTier,
    review_reasons: list[str],
) -> Decision:
    rating = {
        SelectionTier.TOP: 5,
        SelectionTier.SELECT: 4,
        SelectionTier.RESERVE: 3,
        SelectionTier.NONE: None,
    }[tier]
    reason = {
        SelectionTier.TOP: "comparative_top",
        SelectionTier.SELECT: "comparative_select",
        SelectionTier.RESERVE: "comparative_reserve",
        SelectionTier.NONE: "evidence_unavailable",
    }[tier]
    return Decision(
        item_id=item.item_id,
        status=status,
        tier=tier,
        rating_proposal=rating,
        reason_codes=list(dict.fromkeys([reason, *review_reasons])),
        human_reason=(
            "Editorial tier proposed, but technical or visual concerns still require review."
            if status == DecisionStatus.REVIEW
            else (
                "Comparative curation proposal; the score is relative and uncalibrated."
                if tier != SelectionTier.NONE
                else "The available evidence is insufficient for selection."
            )
        ),
        group_ids=list(item.group_ids),
        provenance={
            "stage": "genre_curation",
            "comparative_score": item_score.score,
            "score_components": dict(item_score.components),
            "winner_id": item_score.winner_id,
            "alternative_ids": list(item_score.alternative_ids),
            "profile_versions": dict(item_score.profile_versions),
            "score_calibrated": False,
            "required_asset_ids": list(item.required_asset_ids),
        },
        decision_authority=DecisionAuthority.PROPOSAL,
        user_override=False,
    )


def curate(
    manifest: Manifest,
    groups: list[Group],
    items: list[SelectionItem],
    assessments: list[Assessment],
    brief: Brief,
    overrides: list[Decision],
) -> tuple[list[Decision], Selection]:
    """Build broad selects and an ordered top from item-level evidence."""

    _validate_repetition_limits(brief, groups)
    scores = score_items(manifest, groups, items, assessments, brief)
    scores_by_id = {value.item_id: value for value in scores}
    items_by_id = {item.item_id: item for item in items}
    duplicate_override_ids = _duplicates(
        [override.item_id for override in overrides]
    )
    if duplicate_override_ids:
        raise ValueError(
            f"override item IDs must be unique: {duplicate_override_ids}"
        )
    unknown_override_ids = sorted(
        override.item_id
        for override in overrides
        if override.item_id not in items_by_id
    )
    if unknown_override_ids:
        raise ValueError(
            f"overrides reference unknown items: {unknown_override_ids}"
        )
    manual_by_id = {
        override.item_id: override
        for override in overrides
        if override.decision_authority == DecisionAuthority.USER
        or override.user_override
    }
    assessments_by_asset = {
        assessment.asset_id: assessment for assessment in assessments
    }
    status_and_reasons = {
        item.item_id: _item_status(item, assessments_by_asset, brief)
        for item in items
    }
    statuses = {
        item_id: status for item_id, (status, _reasons) in status_and_reasons.items()
    }
    automatic_select_ids = [
        value.item_id
        for value in scores
        if value.item_id not in manual_by_id
        and statuses[value.item_id] != DecisionStatus.UNASSESSED
        and value.score >= 0
    ]
    forced_top_ids = {
        item_id
        for item_id, decision in manual_by_id.items()
        if decision.status != DecisionStatus.REJECT
        and decision.tier == SelectionTier.TOP
    }
    forced_select_ids = {
        item_id
        for item_id, decision in manual_by_id.items()
        if decision.status != DecisionStatus.REJECT
        and decision.tier in {SelectionTier.TOP, SelectionTier.SELECT}
    }
    required_labels = list(
        dict.fromkeys(
            label.strip().casefold()
            for label in brief.coverage_checklist
            if label.strip()
        )
    )
    coverage_by_item = {
        item.item_id: _coverage_labels(item, assessments_by_asset) for item in items
    }
    ordered_forced_top = [
        value.item_id for value in scores if value.item_id in forced_top_ids
    ]
    forced_covered_labels = {
        label
        for item_id in ordered_forced_top
        for label in coverage_by_item[item_id]
        if label in required_labels
    }
    remaining_required = [
        label for label in required_labels if label not in forced_covered_labels
    ]
    automatic_capacity = (
        0
        if brief.top_k is None
        else max(brief.top_k - len(ordered_forced_top), 0)
    )
    all_groups_by_item = _groups_by_item(groups, items)
    initial_group_counts: dict[str, int] = defaultdict(int)
    for item_id in ordered_forced_top:
        for group_id in all_groups_by_item.get(item_id, set()):
            initial_group_counts[group_id] += 1
    automatic_top_ids = _choose_top(
        [scores_by_id[item_id] for item_id in automatic_select_ids],
        automatic_capacity,
        remaining_required,
        coverage_by_item,
        _competitive_groups_by_item(groups, items),
        all_groups_by_item,
        dict(initial_group_counts),
        brief.repetition_limits,
    )
    top_members = set(ordered_forced_top) | set(automatic_top_ids)
    top_ids = [value.item_id for value in scores if value.item_id in top_members]
    select_members = set(automatic_select_ids) | forced_select_ids
    selects_ids = [
        *top_ids,
        *[
            value.item_id
            for value in scores
            if value.item_id in select_members and value.item_id not in top_members
        ],
    ]

    unmet_constraints: list[str] = []
    if brief.top_k is not None and len(top_ids) < brief.top_k:
        unmet_constraints.append("insufficient_qualified_items")
    if brief.top_k is not None and len(ordered_forced_top) > brief.top_k:
        unmet_constraints.append("manual_top_exceeds_top_k")
    top_group_counts: dict[str, int] = defaultdict(int)
    for item_id in top_ids:
        for group_id in all_groups_by_item.get(item_id, set()):
            top_group_counts[group_id] += 1
    for group_id, limit in sorted(brief.repetition_limits.items()):
        blocked_qualified = any(
            item_id not in top_members
            and item_id in select_members
            and group_id in all_groups_by_item.get(item_id, set())
            for item_id in items_by_id
        )
        if top_group_counts[group_id] > limit or (
            brief.top_k is not None
            and len(top_ids) < brief.top_k
            and top_group_counts[group_id] >= limit
            and blocked_qualified
        ):
            unmet_constraints.append(f"repetition_limit:{group_id}")
    covered_labels = {
        label
        for item_id in top_ids
        for label in coverage_by_item[item_id]
        if label in required_labels
    }
    missing_labels = [
        label for label in required_labels if label not in covered_labels
    ]
    unmet_constraints.extend(f"coverage:{label}" for label in missing_labels)
    present_required = {
        label
        for item_id in selects_ids
        for label in coverage_by_item[item_id]
        if label in required_labels
    }
    if (
        brief.top_k is not None
        and present_required - covered_labels
        and len(top_ids) >= brief.top_k
    ):
        unmet_constraints.append("coverage_exceeds_top_k")

    decisions: list[Decision] = []
    for value in scores:
        item_id = value.item_id
        if item_id in manual_by_id:
            decisions.append(manual_by_id[item_id])
            continue
        if item_id in top_members:
            tier = SelectionTier.TOP
        elif item_id in select_members:
            tier = SelectionTier.SELECT
        elif statuses[item_id] == DecisionStatus.UNASSESSED:
            tier = SelectionTier.NONE
        else:
            tier = SelectionTier.RESERVE
        decisions.append(
            _generated_decision(
                items_by_id[item_id],
                value,
                statuses[item_id],
                tier,
                status_and_reasons[item_id][1],
            )
        )

    protected_group_ids = {
        group.group_id for group in groups if group.protected_set
    }
    selected_group_ids = {
        group_id
        for item_id in selects_ids
        for group_id in items_by_id[item_id].group_ids
        if group_id in protected_group_ids
    }
    selection = Selection(
        selects_ids=selects_ids,
        top_ids=top_ids,
        ranked_ids=[value.item_id for value in scores],
        coverage={
            "requested": required_labels,
            "satisfied": [
                label for label in required_labels if label in covered_labels
            ],
            "missing": missing_labels,
            "items_by_label": {
                label: [
                    item_id
                    for item_id in top_ids
                    if label in coverage_by_item[item_id]
                ]
                for label in required_labels
                if label in covered_labels
            },
        },
        unmet_constraints=unmet_constraints,
        protected_sets=sorted(selected_group_ids),
    )
    return decisions, selection


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


def _read_jsonl(path: Path, model: type[ModelT]) -> list[ModelT]:
    if not path.is_file():
        raise FileNotFoundError(f"required artifact does not exist: {path}")
    result: list[ModelT] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            result.append(model.model_validate_json(line))
        except ValidationError as error:
            raise ValueError(
                f"invalid {path.name} line {line_number}: {error}"
            ) from error
    return result


def _read_manifest(output: Path) -> Manifest:
    return Manifest(assets=_read_jsonl(output / "assets.jsonl", Asset))


def _require_complete_assessment_generation(output: Path) -> None:
    path = output / "assessment-generation.json"
    if not path.is_file():
        raise ValueError("assessment generation marker is missing; run assess again")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"invalid assessment-generation.json: {error}") from error
    if not isinstance(value, dict) or value.get("status") not in {
        "publishing",
        "complete",
    }:
        raise ValueError("invalid assessment-generation.json: unknown status")
    if value["status"] != "complete":
        raise ValueError("assessment generation is incomplete; run assess again")


def _jsonl(values: list[BaseModel]) -> str:
    return "".join(value.model_dump_json() + "\n" for value in values)


def _publish_curation_artifacts(
    output: Path,
    decisions: list[Decision],
    selection: Selection,
    report: dict[str, Any],
) -> None:
    invalidate_report_generation(output, "curation_changed")
    artifacts = {
        output / "decisions.jsonl": _jsonl(decisions),
        output / "selection.json": selection.model_dump_json(indent=2) + "\n",
        output / "curation-report.json": json.dumps(
            report, ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n",
    }
    generation_id = hashlib.sha256(
        "".join(
            f"{path.name}\0{contents}" for path, contents in artifacts.items()
        ).encode("utf-8")
    ).hexdigest()
    marker_path = output / "curation-generation.json"
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


def curate_prepared(output: Path) -> dict[str, Any]:
    """Curate a prepared session and publish one auditable generation."""

    output = output.expanduser().resolve()
    validate_output_layout(output)
    config = load_run_config(output)
    _require_complete_assessment_generation(output)
    manifest = _read_manifest(output)
    groups = _read_json_array(output / "groups.json", Group)
    items = _read_json_array(output / "selection-items.json", SelectionItem)
    assessments = _read_jsonl(output / "assessments.jsonl", Assessment)
    prior_decisions = _read_jsonl(output / "decisions.jsonl", Decision)
    decisions, selection = curate(
        manifest,
        groups,
        items,
        assessments,
        config.brief,
        prior_decisions,
    )
    profiles = _load_profiles(config.brief)
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "completed",
        "items": len(items),
        "selects": len(selection.selects_ids),
        "top": len(selection.top_ids),
        "unmet_constraints": list(selection.unmet_constraints),
        "profile_versions": {
            profile.genre: profile.profile_version for profile in profiles
        },
        "photo_eval_status": "unavailable",
        "photo_metrics": {
            "burst_choice": "unavailable",
            "ndcg": "unavailable",
            "coverage": "unavailable",
        },
    }
    _publish_curation_artifacts(output, decisions, selection, report)
    return report
