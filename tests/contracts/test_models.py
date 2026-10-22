import json
from pathlib import Path

import pytest
from jsonschema import validate as validate_json_schema
from pydantic import ValidationError

from evals.contracts import TrialResult
from silverloupe.cli import doctor_report
from silverloupe.contracts import Assessment, Decision, Selection


def test_selection_rejects_top_item_outside_selects() -> None:
    with pytest.raises(ValidationError, match="top_ids must be a subset"):
        Selection(selects_ids=["item-1"], top_ids=["item-2"])


def test_unassessed_cannot_claim_proven_visual_evaluation() -> None:
    with pytest.raises(ValidationError, match="unassessed"):
        Assessment(
            asset_id="asset-1",
            evaluation_status="unassessed",
            visual_status="evaluated",
            observed_artifacts=["overview-1"],
            visual_criteria={"focus": "confirmed"},
        )


@pytest.mark.parametrize("rating", [None, 0, 1, 5])
def test_rating_accepts_null_and_zero_through_five(rating: int | None) -> None:
    decision = Decision(item_id="item-1", status="keep", rating_proposal=rating)

    assert decision.rating_proposal == rating


@pytest.mark.parametrize("rating", [-1, 6])
def test_rating_rejects_values_outside_zero_through_five(rating: int) -> None:
    with pytest.raises(ValidationError):
        Decision(item_id="item-1", status="keep", rating_proposal=rating)


def test_doctor_reports_core_schema_and_optional_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("silverloupe.cli.shutil.which", lambda name: "/opt/bin/exiftool" if name == "exiftool" else None)
    monkeypatch.setattr("silverloupe.cli.importlib.util.find_spec", lambda name: None)
    monkeypatch.delenv("SILVERLOUPE_VISION_CAPABILITY", raising=False)

    report = doctor_report()

    assert report["core"]["status"] == "ready"
    assert report["schema"]["version"] == 1
    assert report["exiftool"] == {"status": "ready", "path": "/opt/bin/exiftool"}
    assert report["raw"]["status"] == "unavailable"
    assert report["vision"]["status"] == "unavailable"


def test_dataset_manifest_declares_six_unavailable_pilots_and_licensed_fixture() -> None:
    root = Path(__file__).parents[2]
    document = json.loads((root / "evals/dataset-manifest.json").read_text())
    schema = json.loads((root / "evals/dataset-manifest.schema.json").read_text())

    validate_json_schema(document, schema)
    pilots = [dataset for dataset in document["datasets"] if dataset["role"] == "pilot"]
    assert len(pilots) == 6
    assert all(dataset["availability"] == "real-data-unavailable" for dataset in pilots)
    assert document["fixtures"][0]["license"] == "CC0-1.0"


def test_trial_result_is_a_versioned_dev_only_contract(tmp_path: Path) -> None:
    result = TrialResult(
        case_id="reference-contracts",
        host="codex",
        variant="candidate",
        output_dir=str(tmp_path),
        status="passed",
        wall_time_seconds=1.25,
    )

    assert result.schema_version == 1
    assert result.status == "passed"
