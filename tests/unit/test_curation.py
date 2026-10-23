from __future__ import annotations

import hashlib
import math

import pytest

from silverloupe.contracts import Assessment, Asset, Brief, Group, Manifest, SelectionItem
from silverloupe.curation import score_items


def _asset(asset_id: str) -> Asset:
    return Asset(
        asset_id=asset_id,
        relative_path=f"{asset_id}.jpg",
        content_sha256=hashlib.sha256(asset_id.encode()).hexdigest(),
        media_type="image/jpeg",
        decode_status="ready",
    )


def _item(asset_id: str, *, item_id: str | None = None) -> SelectionItem:
    return SelectionItem(
        item_id=item_id or f"item-{asset_id}",
        kind="capture",
        member_asset_ids=[asset_id],
        primary_asset_id=asset_id,
    )


def _assessment(
    asset_id: str,
    *,
    visual_criteria: dict[str, object] | None = None,
    evaluation_status: str = "assessed",
    visual_status: str = "evaluated",
) -> Assessment:
    return Assessment(
        asset_id=asset_id,
        evaluation_status=evaluation_status,
        technical_signals={"decode_status": "ready"},
        visual_status=visual_status,
        visual_criteria=visual_criteria or {},
        observed_artifacts=[f"overview-{asset_id}"] if visual_status == "evaluated" else [],
    )


@pytest.mark.parametrize(
    ("groups", "items", "assessments", "message"),
    [
        (
            [],
            [_item("a", item_id="same"), _item("b", item_id="same")],
            [_assessment("a"), _assessment("b")],
            "item IDs must be unique",
        ),
        (
            [],
            [_item("missing")],
            [_assessment("a")],
            "selection items reference unknown assets",
        ),
        (
            [Group(group_id="bad", type="single", member_ids=["missing"])],
            [_item("a")],
            [_assessment("a")],
            "groups reference unknown assets",
        ),
        (
            [],
            [_item("a")],
            [_assessment("missing")],
            "assessments reference unknown assets",
        ),
        (
            [],
            [_item("a")],
            [_assessment("a"), _assessment("a")],
            "assessment asset IDs must be unique",
        ),
    ],
)
def test_curation_rejects_duplicate_or_unknown_references(
    groups: list[Group],
    items: list[SelectionItem],
    assessments: list[Assessment],
    message: str,
) -> None:
    manifest = Manifest(assets=[_asset("a"), _asset("b")])

    with pytest.raises(ValueError, match=message):
        score_items(manifest, groups, items, assessments, Brief())


def test_numeric_and_categorical_evidence_produce_stable_scores() -> None:
    manifest = Manifest(assets=[_asset("strong"), _asset("weak")])
    items = [_item("strong"), _item("weak")]
    assessments = [
        _assessment(
            "strong",
            visual_criteria={
                "composition": 0.8,
                "light": "acceptable",
                "technical_usability": {"score": 0.5},
            },
        ),
        _assessment(
            "weak",
            visual_criteria={
                "composition": 0.2,
                "light": "concern",
                "technical_usability": {"score": -0.5},
            },
        ),
    ]

    scores = score_items(manifest, [], items, assessments, Brief())

    assert [value.item_id for value in scores] == ["item-strong", "item-weak"]
    assert scores[0].score > scores[1].score
    assert scores[0].components["composition"] == pytest.approx(0.8)
    assert scores[0].components["light"] == pytest.approx(1.0)
    assert scores[0].components["technical_usability"] == pytest.approx(0.5)


def test_nonfinite_or_malformed_score_evidence_is_ignored() -> None:
    manifest = Manifest(assets=[_asset("a")])
    assessment = _assessment(
        "a",
        visual_criteria={
            "composition": math.nan,
            "light": {"score": math.inf},
            "moment": {"unexpected": 1},
            "subject": [1, 2, 3],
        },
    )

    scores = score_items(manifest, [], [_item("a")], [assessment], Brief())

    assert scores[0].components == {"technical_readiness": 0.5}
    assert math.isfinite(scores[0].score)


def test_ties_are_stable_and_record_all_alternatives() -> None:
    manifest = Manifest(assets=[_asset("b"), _asset("a"), _asset("c")])
    items = [_item("b"), _item("a"), _item("c")]
    assessments = [_assessment(asset_id) for asset_id in ("a", "b", "c")]
    group = Group(
        group_id="burst-1",
        type="burst",
        member_ids=["a", "b", "c"],
        confidence_kind="measured",
        confidence_value=0.9,
    )

    scores = score_items(manifest, [group], items, assessments, Brief())

    assert [value.item_id for value in scores] == ["item-a", "item-b", "item-c"]
    assert scores[0].winner_id == "item-a"
    assert scores[0].alternative_ids == ["item-b", "item-c"]
    assert scores[1].winner_id == "item-a"
    assert scores[1].alternative_ids == ["item-a", "item-c"]


def test_unknown_genre_does_not_apply_wedding_portrait_rules() -> None:
    manifest = Manifest(assets=[_asset("eyes-open"), _asset("eyes-closed")])
    items = [_item("eyes-open"), _item("eyes-closed")]
    assessments = [
        _assessment(
            "eyes-open",
            visual_criteria={"eye_state_when_contextually_relevant": "acceptable"},
        ),
        _assessment(
            "eyes-closed",
            visual_criteria={"eye_state_when_contextually_relevant": "concern"},
        ),
    ]

    scores = score_items(
        manifest,
        [],
        items,
        assessments,
        Brief(genres=["underwater-macro"]),
    )

    assert scores[0].score == scores[1].score
    assert all(value.profile_versions == {} for value in scores)
    assert all(
        "eye_state_when_contextually_relevant" not in value.components
        for value in scores
    )
