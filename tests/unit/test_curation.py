from __future__ import annotations

import hashlib
import math

import pytest

from silverloupe.contracts import (
    Assessment,
    Asset,
    Brief,
    Decision,
    DecisionAuthority,
    DecisionStatus,
    Group,
    Manifest,
    SelectionItem,
    SelectionTier,
)
from silverloupe.curation import curate, score_items


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


@pytest.mark.parametrize(
    ("top_k", "expected_top", "expected_unmet"),
    [
        (0, [], []),
        (1, ["item-strong"], []),
        (5, ["item-strong", "item-weak"], ["insufficient_qualified_items"]),
        (None, [], []),
    ],
)
def test_top_k_boundaries_and_subset_contract(
    top_k: int | None,
    expected_top: list[str],
    expected_unmet: list[str],
) -> None:
    manifest = Manifest(assets=[_asset("strong"), _asset("weak")])
    items = [_item("strong"), _item("weak")]
    assessments = [
        _assessment("strong", visual_criteria={"composition": 1.0}),
        _assessment("weak", visual_criteria={"composition": 0.0}),
    ]

    decisions, selection = curate(
        manifest, [], items, assessments, Brief(top_k=top_k), []
    )

    assert selection.top_ids == expected_top
    assert set(selection.top_ids).issubset(selection.selects_ids)
    assert selection.ranked_ids == ["item-strong", "item-weak"]
    assert selection.unmet_constraints == expected_unmet
    assert len(decisions) == 2


def test_top_k_counts_composite_item_not_required_assets() -> None:
    assets = [_asset(asset_id) for asset_id in ("left", "middle", "right")]
    manifest = Manifest(assets=assets)
    group = Group(
        group_id="hdr-1",
        type="hdr",
        member_ids=[asset.asset_id for asset in assets],
        protected_set=True,
    )
    item = SelectionItem(
        item_id="item-hdr",
        kind="composite",
        member_asset_ids=[asset.asset_id for asset in assets],
        primary_asset_id="middle",
        required_asset_ids=[asset.asset_id for asset in assets],
        group_ids=[group.group_id],
    )

    decisions, selection = curate(
        manifest,
        [group],
        [item],
        [_assessment(asset.asset_id) for asset in assets],
        Brief(top_k=1),
        [],
    )

    assert selection.top_ids == ["item-hdr"]
    assert selection.selects_ids == ["item-hdr"]
    assert selection.protected_sets == ["hdr-1"]
    assert decisions[0].provenance["required_asset_ids"] == ["left", "middle", "right"]


def test_empty_and_all_unassessed_inputs_remain_honest() -> None:
    empty_decisions, empty_selection = curate(
        Manifest(assets=[]), [], [], [], Brief(top_k=1), []
    )
    assert empty_decisions == []
    assert empty_selection.selects_ids == []
    assert empty_selection.top_ids == []
    assert empty_selection.unmet_constraints == ["insufficient_qualified_items"]

    manifest = Manifest(assets=[_asset("a")])
    decisions, selection = curate(
        manifest,
        [],
        [_item("a")],
        [
            _assessment(
                "a",
                evaluation_status="unassessed",
                visual_status="not_evaluated",
            )
        ],
        Brief(top_k=1),
        [],
    )
    assert selection.selects_ids == []
    assert selection.top_ids == []
    assert selection.ranked_ids == ["item-a"]
    assert selection.unmet_constraints == ["insufficient_qualified_items"]
    assert decisions[0].status == DecisionStatus.UNASSESSED
    assert decisions[0].tier == SelectionTier.NONE


def test_diversity_penalty_prefers_distinct_item_over_second_burst_frame() -> None:
    manifest = Manifest(
        assets=[_asset("burst-a"), _asset("burst-b"), _asset("distinct")]
    )
    items = [_item("burst-a"), _item("burst-b"), _item("distinct")]
    assessments = [
        _assessment("burst-a", visual_criteria={"composition": 1.0}),
        _assessment("burst-b", visual_criteria={"composition": 0.9}),
        _assessment("distinct", visual_criteria={"composition": 0.4}),
    ]
    group = Group(
        group_id="burst-1",
        type="burst",
        member_ids=["burst-a", "burst-b"],
        confidence_kind="measured",
        confidence_value=0.9,
    )

    _decisions, selection = curate(
        manifest, [group], items, assessments, Brief(top_k=2), []
    )

    assert selection.top_ids == ["item-burst-a", "item-distinct"]
    assert selection.ranked_ids[:2] == ["item-burst-a", "item-burst-b"]


def test_mandatory_coverage_is_selected_when_feasible() -> None:
    manifest = Manifest(assets=[_asset("ceremony"), _asset("portraits"), _asset("detail")])
    items = [_item("ceremony"), _item("portraits"), _item("detail")]
    assessments = [
        _assessment(
            "ceremony",
            visual_criteria={"composition": 0.6, "coverage": ["ceremony"]},
        ),
        _assessment(
            "portraits",
            visual_criteria={"composition": 0.5, "coverage": ["portraits"]},
        ),
        _assessment("detail", visual_criteria={"composition": 1.0}),
    ]

    _decisions, selection = curate(
        manifest,
        [],
        items,
        assessments,
        Brief(top_k=2, coverage_checklist=["ceremony", "portraits"]),
        [],
    )

    assert selection.top_ids == ["item-ceremony", "item-portraits"]
    assert selection.coverage == {
        "requested": ["ceremony", "portraits"],
        "satisfied": ["ceremony", "portraits"],
        "missing": [],
        "items_by_label": {
            "ceremony": ["item-ceremony"],
            "portraits": ["item-portraits"],
        },
    }
    assert selection.unmet_constraints == []


def test_missing_coverage_and_coverage_vs_k_conflict_are_disclosed() -> None:
    manifest = Manifest(assets=[_asset("ceremony"), _asset("portraits")])
    items = [_item("ceremony"), _item("portraits")]
    assessments = [
        _assessment(
            "ceremony",
            visual_criteria={"composition": 1.0, "coverage": ["ceremony"]},
        ),
        _assessment(
            "portraits",
            visual_criteria={"composition": 0.5, "coverage": ["portraits"]},
        ),
    ]

    _decisions, selection = curate(
        manifest,
        [],
        items,
        assessments,
        Brief(top_k=1, coverage_checklist=["ceremony", "portraits", "reception"]),
        [],
    )

    assert selection.top_ids == ["item-ceremony"]
    assert selection.coverage["satisfied"] == ["ceremony"]
    assert selection.coverage["missing"] == ["portraits", "reception"]
    assert set(selection.unmet_constraints) == {
        "coverage:portraits",
        "coverage:reception",
        "coverage_exceeds_top_k",
    }


def _override(
    item_id: str,
    *,
    status: DecisionStatus = DecisionStatus.KEEP,
    tier: SelectionTier,
    rating: int | None,
) -> Decision:
    return Decision(
        item_id=item_id,
        status=status,
        tier=tier,
        rating_proposal=rating,
        tags=[f"manual:{tier.value}"],
        reason_codes=["manual_choice"],
        human_reason=f"User chose {tier.value}",
        provenance={"source": "test-user"},
        decision_authority=DecisionAuthority.USER,
        user_override=True,
    )


def test_manual_overrides_force_membership_and_preserve_decisions_verbatim() -> None:
    asset_ids = ["top", "select", "reserve", "reject"]
    manifest = Manifest(assets=[_asset(asset_id) for asset_id in asset_ids])
    items = [_item(asset_id) for asset_id in asset_ids]
    assessments = [
        _assessment(asset_id, visual_criteria={"composition": index / 10})
        for index, asset_id in enumerate(asset_ids, start=1)
    ]
    overrides = [
        _override("item-top", tier=SelectionTier.TOP, rating=5),
        _override("item-select", tier=SelectionTier.SELECT, rating=4),
        _override("item-reserve", tier=SelectionTier.RESERVE, rating=3),
        _override(
            "item-reject",
            status=DecisionStatus.REJECT,
            tier=SelectionTier.NONE,
            rating=None,
        ),
    ]

    decisions, selection = curate(
        manifest, [], items, assessments, Brief(top_k=1), overrides
    )

    assert selection.top_ids == ["item-top"]
    assert selection.selects_ids == ["item-top", "item-select"]
    assert {decision.item_id: decision for decision in decisions} == {
        decision.item_id: decision for decision in overrides
    }


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            [
                _override("item-a", tier=SelectionTier.TOP, rating=5),
                _override("item-a", tier=SelectionTier.SELECT, rating=4),
            ],
            "override item IDs must be unique",
        ),
        (
            [_override("missing", tier=SelectionTier.TOP, rating=5)],
            "overrides reference unknown items",
        ),
    ],
)
def test_invalid_manual_override_references_are_rejected(
    overrides: list[Decision], message: str
) -> None:
    manifest = Manifest(assets=[_asset("a")])

    with pytest.raises(ValueError, match=message):
        curate(
            manifest,
            [],
            [_item("a")],
            [_assessment("a")],
            Brief(top_k=1),
            overrides,
        )


def test_manual_top_beyond_k_is_preserved_and_disclosed() -> None:
    manifest = Manifest(assets=[_asset("a"), _asset("b")])
    overrides = [
        _override("item-a", tier=SelectionTier.TOP, rating=5),
        _override("item-b", tier=SelectionTier.TOP, rating=5),
    ]

    _decisions, selection = curate(
        manifest,
        [],
        [_item("a"), _item("b")],
        [_assessment("a"), _assessment("b")],
        Brief(top_k=1),
        overrides,
    )

    assert selection.top_ids == ["item-a", "item-b"]
    assert set(selection.top_ids).issubset(selection.selects_ids)
    assert "manual_top_exceeds_top_k" in selection.unmet_constraints
