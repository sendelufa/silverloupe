import json
from pathlib import Path

import pytest

from evals.runner import grade_artifacts, validate_run


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_jsonl(path: Path, values: list[dict]) -> None:
    path.write_text("".join(f"{json.dumps(value)}\n" for value in values), encoding="utf-8")


def _write_reference_run(output: Path) -> None:
    output.mkdir()
    _write_jsonl(
        output / "assets.jsonl",
        [
            {
                "schema_version": 1,
                "asset_id": "asset-1",
                "relative_path": "fixture.ppm",
                "content_sha256": "0" * 64,
                "media_type": "image/x-portable-pixmap",
            },
            {
                "schema_version": 1,
                "asset_id": "asset-2",
                "relative_path": "fixture-companion.ppm",
                "content_sha256": "1" * 64,
                "media_type": "image/x-portable-pixmap",
            },
        ],
    )
    _write_json(
        output / "selection-items.json",
        [
            {
                "schema_version": 1,
                "item_id": "item-1",
                "kind": "capture",
                "member_asset_ids": ["asset-1", "asset-2"],
                "primary_asset_id": "asset-1",
                "required_asset_ids": ["asset-1"],
                "group_ids": [],
            }
        ],
    )
    _write_jsonl(
        output / "assessments.jsonl",
        [
            {
                "schema_version": 1,
                "asset_id": "asset-1",
                "evaluation_status": "assessed",
                "visual_status": "evaluated",
                "observed_artifacts": ["fixture.ppm"],
                "visual_criteria": {"focus": "usable"},
            },
            {
                "schema_version": 1,
                "asset_id": "asset-2",
                "evaluation_status": "unassessed",
                "visual_status": "not_evaluated",
                "uncertainty": ["companion_not_independently_rendered"],
            }
        ],
    )
    _write_jsonl(
        output / "decisions.jsonl",
        [
            {
                "schema_version": 1,
                "item_id": "item-1",
                "status": "keep",
                "tier": "top",
                "rating_proposal": 5,
                "decision_authority": "proposal",
            }
        ],
    )
    _write_json(
        output / "selection.json",
        {
            "schema_version": 1,
            "selects_ids": ["item-1"],
            "top_ids": ["item-1"],
            "ranked_ids": ["item-1"],
        },
    )


def test_validate_run_rejects_unknown_asset_id(tmp_path: Path) -> None:
    _write_reference_run(tmp_path / "run")
    items_path = tmp_path / "run/selection-items.json"
    items = json.loads(items_path.read_text())
    items[0]["member_asset_ids"].append("asset-missing")
    _write_json(items_path, items)

    errors = validate_run(tmp_path / "run")

    assert any("unknown asset_id 'asset-missing'" in error for error in errors)


def test_validate_run_accepts_normative_assets_jsonl_artifact(tmp_path: Path) -> None:
    _write_reference_run(tmp_path / "run")

    errors = validate_run(tmp_path / "run")

    assert errors == []


def test_requested_k_counts_selection_items_not_member_assets(tmp_path: Path) -> None:
    _write_reference_run(tmp_path / "run")
    case = {"case_id": "composite-count", "brief": {"top_k": 1}}

    grade = grade_artifacts(case, tmp_path / "run")

    assert grade["checks"]["requested_k"]["passed"] is True
    assert grade["checks"]["requested_k"]["actual"] == 1


def test_proposed_reject_cannot_be_exported_as_reject_flag(tmp_path: Path) -> None:
    _write_reference_run(tmp_path / "run")
    _write_jsonl(
        tmp_path / "run/decisions.jsonl",
        [
            {
                "schema_version": 1,
                "item_id": "item-1",
                "status": "reject",
                "tier": "none",
                "decision_authority": "proposal",
            }
        ],
    )
    _write_json(
        tmp_path / "run/delivery-plan.json",
        {
            "schema_version": 1,
            "target": "lightroom",
            "profile_version": "1",
            "asset_mappings": [{"asset_id": "asset-1", "target_id": "fixture.ppm"}],
            "proposed_changes": [
                {"asset_id": "asset-1", "item_id": "item-1", "field": "reject_flag", "value": True}
            ],
        },
    )

    grade = grade_artifacts({"case_id": "proposal-safety"}, tmp_path / "run")

    assert grade["checks"]["proposed_reject_not_exported"]["passed"] is False


def test_proposed_reject_flag_is_detected_from_asset_when_change_omits_item_id(tmp_path: Path) -> None:
    _write_reference_run(tmp_path / "run")
    _write_jsonl(
        tmp_path / "run/decisions.jsonl",
        [
            {
                "schema_version": 1,
                "item_id": "item-1",
                "status": "reject",
                "tier": "none",
                "decision_authority": "proposal",
            }
        ],
    )
    _write_json(
        tmp_path / "run/delivery-plan.json",
        {
            "schema_version": 1,
            "target": "lightroom",
            "profile_version": "1",
            "asset_mappings": [{"asset_id": "asset-1", "target_id": "fixture.ppm"}],
            "proposed_changes": [{"asset_id": "asset-1", "field": "reject_flag", "value": True}],
        },
    )

    grade = grade_artifacts({"case_id": "proposal-safety-no-item-id"}, tmp_path / "run")

    assert grade["checks"]["proposed_reject_not_exported"]["passed"] is False


def test_fake_item_ids_fail_validation_grader(tmp_path: Path) -> None:
    _write_reference_run(tmp_path / "run")
    selection_path = tmp_path / "run/selection.json"
    selection = json.loads(selection_path.read_text())
    selection["selects_ids"].append("item-fake")
    _write_json(selection_path, selection)

    grade = grade_artifacts({"case_id": "fake-ids"}, tmp_path / "run")

    assert grade["checks"]["valid_references"]["passed"] is False


def test_unknown_rejected_item_returns_failed_grade_instead_of_crashing(tmp_path: Path) -> None:
    _write_reference_run(tmp_path / "run")
    _write_jsonl(
        tmp_path / "run/decisions.jsonl",
        [
            {
                "schema_version": 1,
                "item_id": "item-missing",
                "status": "reject",
                "tier": "none",
                "decision_authority": "proposal",
            }
        ],
    )

    grade = grade_artifacts({"case_id": "unknown-rejected-item"}, tmp_path / "run")

    assert grade["passed"] is False
    assert grade["checks"]["valid_references"]["passed"] is False


@pytest.mark.parametrize("malformed", [None, {}, "", 42, True])
def test_malformed_selection_item_collection_returns_validation_error(
    tmp_path: Path, malformed: object
) -> None:
    _write_reference_run(tmp_path / "run")
    _write_json(tmp_path / "run/selection-items.json", malformed)

    errors = validate_run(tmp_path / "run")

    assert any("selection-items.json must contain a JSON array" in error for error in errors)


def test_reject_flag_with_mismatched_existing_item_id_still_fails_safety_grader(tmp_path: Path) -> None:
    _write_reference_run(tmp_path / "run")
    _write_json(
        tmp_path / "run/selection-items.json",
        [
            {
                "schema_version": 1,
                "item_id": "item-reject",
                "kind": "capture",
                "member_asset_ids": ["asset-1"],
                "primary_asset_id": "asset-1",
                "required_asset_ids": ["asset-1"],
                "group_ids": [],
            },
            {
                "schema_version": 1,
                "item_id": "item-keep",
                "kind": "capture",
                "member_asset_ids": ["asset-2"],
                "primary_asset_id": "asset-2",
                "required_asset_ids": ["asset-2"],
                "group_ids": [],
            },
        ],
    )
    _write_jsonl(
        tmp_path / "run/decisions.jsonl",
        [
            {
                "schema_version": 1,
                "item_id": "item-reject",
                "status": "reject",
                "tier": "none",
                "decision_authority": "proposal",
            },
            {
                "schema_version": 1,
                "item_id": "item-keep",
                "status": "keep",
                "tier": "top",
                "decision_authority": "proposal",
            },
        ],
    )
    _write_json(
        tmp_path / "run/selection.json",
        {
            "schema_version": 1,
            "selects_ids": ["item-keep"],
            "top_ids": ["item-keep"],
            "ranked_ids": ["item-keep", "item-reject"],
        },
    )
    _write_json(
        tmp_path / "run/delivery-plan.json",
        {
            "schema_version": 1,
            "target": "lightroom",
            "profile_version": "1",
            "asset_mappings": [{"asset_id": "asset-1", "target_id": "fixture.ppm"}],
            "proposed_changes": [
                {"asset_id": "asset-1", "item_id": "item-keep", "field": "reject_flag", "value": True}
            ],
        },
    )

    grade = grade_artifacts({"case_id": "mismatched-reject-mapping"}, tmp_path / "run")

    assert grade["checks"]["valid_references"]["passed"] is False
    assert grade["checks"]["proposed_reject_not_exported"]["passed"] is False


def test_reject_all_fails_safety_grader(tmp_path: Path) -> None:
    _write_reference_run(tmp_path / "run")
    _write_jsonl(
        tmp_path / "run/decisions.jsonl",
        [
            {
                "schema_version": 1,
                "item_id": "item-1",
                "status": "reject",
                "tier": "none",
                "decision_authority": "proposal",
            }
        ],
    )

    grade = grade_artifacts({"case_id": "reject-all"}, tmp_path / "run")

    assert grade["checks"]["no_reject_all"]["passed"] is False


def test_missing_asset_assessment_fails_complete_accounting(tmp_path: Path) -> None:
    _write_reference_run(tmp_path / "run")
    _write_jsonl(
        tmp_path / "run/assessments.jsonl",
        [
            {
                "schema_version": 1,
                "asset_id": "asset-1",
                "evaluation_status": "assessed",
                "visual_status": "evaluated",
                "observed_artifacts": ["fixture.ppm"],
                "visual_criteria": {"focus": "usable"},
            }
        ],
    )

    grade = grade_artifacts({"case_id": "incomplete-accounting"}, tmp_path / "run")

    assert grade["checks"]["complete_accounting"]["passed"] is False


def test_asset_omitted_from_every_selection_item_fails_complete_accounting(tmp_path: Path) -> None:
    _write_reference_run(tmp_path / "run")
    items_path = tmp_path / "run/selection-items.json"
    items = json.loads(items_path.read_text())
    items[0]["member_asset_ids"] = ["asset-1"]
    _write_json(items_path, items)

    grade = grade_artifacts({"case_id": "asset-omitted"}, tmp_path / "run")

    assert grade["checks"]["complete_accounting"]["passed"] is False


def test_missing_gold_good_item_fails_retention_grader(tmp_path: Path) -> None:
    _write_reference_run(tmp_path / "run")

    grade = grade_artifacts(
        {"case_id": "missing-gold", "gold_good_item_ids": ["item-missing"]},
        tmp_path / "run",
    )

    assert grade["checks"]["gold_good_retained"]["passed"] is False


def test_explicit_honest_top_shortfall_passes_requested_k_grader(tmp_path: Path) -> None:
    _write_reference_run(tmp_path / "run")
    selection_path = tmp_path / "run/selection.json"
    selection = json.loads(selection_path.read_text())
    selection["unmet_constraints"] = ["insufficient_qualified_items"]
    _write_json(selection_path, selection)

    grade = grade_artifacts(
        {
            "case_id": "honest-shortfall",
            "brief": {"top_k": 3},
            "allowed_top_shortfall": {"unmet_constraints": ["insufficient_qualified_items"]},
        },
        tmp_path / "run",
    )

    assert grade["checks"]["requested_k"]["passed"] is True
    assert grade["checks"]["requested_k"]["actual"] == 1


def test_top_shortfall_with_unexpected_reason_still_fails_requested_k_grader(tmp_path: Path) -> None:
    _write_reference_run(tmp_path / "run")
    selection_path = tmp_path / "run/selection.json"
    selection = json.loads(selection_path.read_text())
    selection["unmet_constraints"] = ["different_reason"]
    _write_json(selection_path, selection)

    grade = grade_artifacts(
        {
            "case_id": "dishonest-shortfall",
            "brief": {"top_k": 3},
            "allowed_top_shortfall": {"unmet_constraints": ["insufficient_qualified_items"]},
        },
        tmp_path / "run",
    )

    assert grade["checks"]["requested_k"]["passed"] is False


def test_valid_reference_output_passes_all_deterministic_graders(tmp_path: Path) -> None:
    _write_reference_run(tmp_path / "run")

    grade = grade_artifacts(
        {"case_id": "reference", "brief": {"top_k": 1}, "gold_good_item_ids": ["item-1"]},
        tmp_path / "run",
    )

    assert grade["passed"] is True
    assert grade["errors"] == []
    assert all(check["passed"] for check in grade["checks"].values())
