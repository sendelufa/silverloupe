from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from silverloupe.cli import main
from silverloupe.contracts import (
    Assessment,
    Asset,
    Brief,
    Decision,
    DecisionAuthority,
    DecisionStatus,
    Manifest,
    RunConfig,
    SelectionItem,
)
from silverloupe.state import write_session
from silverloupe.vision.base import VisionObservation, VisionResponse
from silverloupe.vision.host_exchange import (
    apply_vision_response,
    build_vision_request,
    parse_vision_response,
    validate_vision_request_inputs,
)


def _asset(asset_id: str, preview: str | None = None) -> Asset:
    return Asset(
        asset_id=asset_id,
        relative_path=f"{asset_id}.jpg",
        content_sha256=hashlib.sha256(asset_id.encode()).hexdigest(),
        media_type="image/jpeg",
        decode_status="ready",
        preview_provenance={"status": "ready", "path": preview} if preview else None,
    )


def _config(output: Path) -> RunConfig:
    return RunConfig(
        source_roots=[str(output / "source")],
        output_root=str(output),
        brief=Brief(),
        rendering_policy={},
        budget={"vision_policy": {"max_retries": 2}},
    )


def _assessment(asset_id: str) -> Assessment:
    return Assessment(
        asset_id=asset_id,
        evaluation_status="assessed",
        technical_signals={"decode_status": "ready"},
        visual_status="not_evaluated",
    )


def _prepared_request(tmp_path: Path):
    relative = "previews/a.jpg"
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (800, 600), "gray").save(path, "JPEG")
    manifest = Manifest(assets=[_asset("a", relative)])
    assessments = [_assessment("a")]
    request = build_vision_request(manifest, assessments, Brief(), _config(tmp_path))
    return manifest, assessments, request


def _complete_criteria(request, **overrides: str) -> dict[str, str]:
    criteria = {name: "acceptable" for name in request.assets[0].criteria}
    criteria.update(overrides)
    return criteria


def test_request_contains_stable_output_relative_observed_artifact(tmp_path: Path) -> None:
    _manifest, _assessments, first = _prepared_request(tmp_path)
    _manifest, _assessments, second = _prepared_request(tmp_path)

    assert first.request_id == second.request_id
    assert first.attempt == 1
    assert first.max_retries == 2
    assert len(first.artifacts) == 1
    assert first.artifacts[0].asset_id == "a"
    assert first.artifacts[0].path == "previews/a.jpg"
    assert first.assets[0].artifact_ids == [first.artifacts[0].artifact_id]


def test_fake_observed_artifact_id_is_rejected(tmp_path: Path) -> None:
    _manifest, assessments, request = _prepared_request(tmp_path)
    response = VisionResponse(
        request_id=request.request_id,
        status="completed",
        observations=[
            VisionObservation(
                asset_id="a",
                observed_artifact_ids=["not-in-request"],
                visual_criteria=_complete_criteria(request),
            )
        ],
    )

    with pytest.raises(ValueError, match="unknown observed artifact"):
        apply_vision_response(request, response, assessments)


def test_region_evidence_must_reference_an_observed_artifact(tmp_path: Path) -> None:
    _manifest, assessments, request = _prepared_request(tmp_path)
    artifact_id = request.artifacts[0].artifact_id
    response = VisionResponse(
        request_id=request.request_id,
        status="completed",
        observations=[
            VisionObservation(
                asset_id="a",
                observed_artifact_ids=[artifact_id],
                visual_criteria=_complete_criteria(request),
                region_evidence=[
                    {
                        "artifact_id": "other-artifact",
                        "region": [0.1, 0.1, 0.5, 0.5],
                        "finding": "subject detail is inconclusive",
                    }
                ],
            )
        ],
    )

    with pytest.raises(ValueError, match="region evidence"):
        apply_vision_response(request, response, assessments)


def test_valid_response_imports_only_artifact_bound_visual_evidence(tmp_path: Path) -> None:
    _manifest, assessments, request = _prepared_request(tmp_path)
    artifact_id = request.artifacts[0].artifact_id
    response = VisionResponse(
        request_id=request.request_id,
        status="completed",
        observations=[
            VisionObservation(
                asset_id="a",
                observed_artifact_ids=[artifact_id],
                visual_criteria=_complete_criteria(
                    request,
                    eye_state_when_contextually_relevant="not_applicable",
                ),
                region_evidence=[
                    {
                        "artifact_id": artifact_id,
                        "region": [0.1, 0.1, 0.5, 0.5],
                        "finding": "main subject edge is visible",
                    }
                ],
                intent_hypotheses=["intentional_soft_focus"],
                uncertainty=["intent_not_confirmed"],
            )
        ],
    )

    result = apply_vision_response(request, response, assessments)

    assert result.status == "completed"
    assert result.next_request is None
    assert result.assessments[0].visual_status == "evaluated"
    assert result.assessments[0].observed_artifacts == [artifact_id]
    assert result.assessments[0].region_evidence[0]["finding"] == "main subject edge is visible"


def test_invalid_json_never_becomes_success() -> None:
    with pytest.raises(ValueError, match="invalid vision response JSON"):
        parse_vision_response('{"status":"completed",')


@pytest.mark.parametrize("criteria", [{}, {"focus_on_relevant_subject": "bad"}])
def test_empty_or_unrecognized_visual_results_are_invalid(criteria: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        VisionObservation(
            asset_id="a",
            observed_artifact_ids=["overview-a"],
            visual_criteria=criteria,
        )


def test_missing_requested_criteria_cannot_complete_an_observation(tmp_path: Path) -> None:
    _manifest, assessments, request = _prepared_request(tmp_path)
    artifact_id = request.artifacts[0].artifact_id
    response = VisionResponse(
        request_id=request.request_id,
        status="completed",
        observations=[
            VisionObservation(
                asset_id="a",
                observed_artifact_ids=[artifact_id],
                visual_criteria={request.assets[0].criteria[0]: "acceptable"},
            )
        ],
    )

    with pytest.raises(ValueError, match="criteria do not match"):
        apply_vision_response(request, response, assessments)


def test_insufficient_visual_result_remains_requestable_partial(tmp_path: Path) -> None:
    manifest, assessments, request = _prepared_request(tmp_path)
    artifact_id = request.artifacts[0].artifact_id
    response = VisionResponse(
        request_id=request.request_id,
        status="completed",
        observations=[
            VisionObservation(
                asset_id="a",
                observed_artifact_ids=[artifact_id],
                visual_criteria=_complete_criteria(
                    request, focus_on_relevant_subject="insufficient_evidence"
                ),
            )
        ],
    )

    result = apply_vision_response(request, response, assessments)
    next_request = build_vision_request(
        manifest, result.assessments, Brief(), _config(tmp_path)
    )

    assert result.status == "partial"
    assert result.assessments[0].visual_status == "insufficient_evidence"
    assert next_request.assets[0].asset_id == "a"


def test_request_is_invalidated_when_preview_bytes_change(tmp_path: Path) -> None:
    manifest, assessments, request = _prepared_request(tmp_path)
    Image.new("RGB", (800, 600), "black").save(tmp_path / "previews/a.jpg", "JPEG")

    with pytest.raises(ValueError, match="inputs changed"):
        validate_vision_request_inputs(
            request, manifest, assessments, Brief(), _config(tmp_path)
        )


def test_request_is_invalidated_when_manifest_content_changes(tmp_path: Path) -> None:
    manifest, assessments, request = _prepared_request(tmp_path)
    changed = manifest.model_copy(
        update={
            "assets": [
                manifest.assets[0].model_copy(update={"content_sha256": "f" * 64})
            ]
        }
    )

    with pytest.raises(ValueError, match="inputs changed"):
        validate_vision_request_inputs(
            request, changed, assessments, Brief(), _config(tmp_path)
        )


def test_completed_requested_subset_is_partial_when_other_asset_is_unassessed(
    tmp_path: Path,
) -> None:
    manifest, assessments, request = _prepared_request(tmp_path)
    assessments.append(
        Assessment(
            asset_id="unsupported",
            evaluation_status="unassessed",
            technical_signals={"decode_status": "unsupported"},
            visual_status="not_evaluated",
            uncertainty=["decode_unavailable"],
        )
    )
    artifact_id = request.artifacts[0].artifact_id
    response = VisionResponse(
        request_id=request.request_id,
        status="completed",
        observations=[
            VisionObservation(
                asset_id="a",
                observed_artifact_ids=[artifact_id],
                visual_criteria=_complete_criteria(request),
            )
        ],
    )

    result = apply_vision_response(request, response, assessments)

    assert result.status == "partial"
    assert result.assessments[1].evaluation_status == "unassessed"


def test_nonfinite_region_coordinates_are_rejected_as_invalid_json() -> None:
    payload = json.dumps(
        {
            "schema_version": 1,
            "request_id": "vision-a",
            "status": "completed",
            "observations": [
                {
                    "schema_version": 1,
                    "asset_id": "a",
                    "observed_artifact_ids": ["overview-a"],
                    "visual_criteria": {"focus_on_relevant_subject": "acceptable"},
                    "region_evidence": [
                        {
                            "schema_version": 1,
                            "artifact_id": "overview-a",
                            "region": [float("nan"), 0.1, 0.5, 0.5],
                            "finding": "invalid coordinate",
                        }
                    ],
                    "intent_hypotheses": [],
                    "uncertainty": [],
                }
            ],
            "error": None,
        }
    )

    with pytest.raises(ValueError, match="invalid vision response JSON"):
        parse_vision_response(payload)


def test_transient_errors_retry_twice_then_end_partial_review(tmp_path: Path) -> None:
    _manifest, assessments, request = _prepared_request(tmp_path)
    response = VisionResponse(
        request_id=request.request_id,
        status="transient_error",
        observations=[],
        error="host vision temporarily unavailable",
    )

    first = apply_vision_response(request, response, assessments)
    assert first.status == "retry_required"
    assert first.next_request is not None and first.next_request.attempt == 2

    second_response = response.model_copy(update={"request_id": first.next_request.request_id})
    second = apply_vision_response(first.next_request, second_response, assessments)
    assert second.status == "retry_required"
    assert second.next_request is not None and second.next_request.attempt == 3

    third_response = response.model_copy(update={"request_id": second.next_request.request_id})
    third = apply_vision_response(second.next_request, third_response, assessments)
    assert third.status == "partial"
    assert third.next_request is None
    assert third.assessments[0].visual_status == "insufficient_evidence"
    assert "vision_unavailable_after_retries" in third.assessments[0].uncertainty


def test_completed_response_requires_every_requested_asset(tmp_path: Path) -> None:
    _manifest, assessments, request = _prepared_request(tmp_path)
    response = VisionResponse(
        request_id=request.request_id,
        status="completed",
        observations=[],
    )

    with pytest.raises(ValueError, match="missing observations"):
        apply_vision_response(request, response, assessments)


def test_cli_vision_round_trip_rejects_invalid_then_imports_valid_response(
    tmp_path: Path, capsys
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    relative = "previews/a.jpg"
    preview = output / relative
    preview.parent.mkdir()
    Image.new("RGB", (800, 600), "gray").save(preview, "JPEG")
    asset = _asset("a", relative)
    write_session(output, _config(output), "vision-session")
    (output / "assets.jsonl").write_text(asset.model_dump_json() + "\n", encoding="utf-8")
    (output / "groups.json").write_text("[]\n", encoding="utf-8")
    item = SelectionItem(
        item_id="item-a", kind="capture", member_asset_ids=["a"], primary_asset_id="a"
    )
    (output / "selection-items.json").write_text(
        json.dumps([item.model_dump(mode="json")]) + "\n", encoding="utf-8"
    )

    assert main(["assess", str(output)]) == 3
    capsys.readouterr()
    assert main(["vision-request", str(output)]) == 0
    capsys.readouterr()
    request = json.loads((output / "vision-request.json").read_text(encoding="utf-8"))

    invalid_path = tmp_path / "invalid-response.json"
    invalid_path.write_text("not-json", encoding="utf-8")
    before = (output / "assessments.jsonl").read_bytes()
    assert main(["vision-import", str(output), str(invalid_path)]) == 2
    assert "invalid vision response JSON" in capsys.readouterr().err
    assert (output / "assessments.jsonl").read_bytes() == before

    valid_path = tmp_path / "valid-response.json"
    valid_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "request_id": request["request_id"],
                "status": "completed",
                "observations": [
                    {
                        "asset_id": "a",
                        "observed_artifact_ids": [request["artifacts"][0]["artifact_id"]],
                        "visual_criteria": {
                            name: "acceptable" for name in request["assets"][0]["criteria"]
                        },
                        "region_evidence": [],
                        "intent_hypotheses": [],
                        "uncertainty": [],
                    }
                ],
                "error": None,
            }
        ),
        encoding="utf-8",
    )

    assert main(["vision-import", str(output), str(valid_path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    imported = Assessment.model_validate_json(
        (output / "assessments.jsonl").read_text(encoding="utf-8").strip()
    )
    assert summary["status"] == "completed"
    assert imported.visual_status == "evaluated"

    user_decision = Decision(
        item_id="item-a",
        status=DecisionStatus.KEEP,
        reason_codes=["user_confirmed"],
        decision_authority=DecisionAuthority.USER,
        user_override=True,
    )
    (output / "decisions.jsonl").write_text(
        user_decision.model_dump_json() + "\n", encoding="utf-8"
    )

    assert main(["assess", str(output)]) == 0
    capsys.readouterr()
    preserved_assessment = Assessment.model_validate_json(
        (output / "assessments.jsonl").read_text(encoding="utf-8").strip()
    )
    preserved_decision = Decision.model_validate_json(
        (output / "decisions.jsonl").read_text(encoding="utf-8").strip()
    )
    assert preserved_assessment.visual_status == "evaluated"
    assert preserved_decision.decision_authority == DecisionAuthority.USER
    assert preserved_decision.user_override is True

    Image.new("RGB", (800, 600), "black").save(preview, "JPEG")
    assert main(["assess", str(output)]) == 3
    capsys.readouterr()
    invalidated_assessment = Assessment.model_validate_json(
        (output / "assessments.jsonl").read_text(encoding="utf-8").strip()
    )
    invalidated_decision = Decision.model_validate_json(
        (output / "decisions.jsonl").read_text(encoding="utf-8").strip()
    )
    vision_status = json.loads(
        (output / "vision-status.json").read_text(encoding="utf-8")
    )
    assert invalidated_assessment.visual_status == "not_evaluated"
    assert invalidated_decision.decision_authority == DecisionAuthority.USER
    assert vision_status["status"] == "invalidated"


def test_cli_import_stays_partial_when_an_unsupported_asset_remains(
    tmp_path: Path, capsys
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    relative = "previews/ready.jpg"
    preview = output / relative
    preview.parent.mkdir()
    Image.new("RGB", (800, 600), "gray").save(preview, "JPEG")
    ready = _asset("ready", relative)
    unsupported = _asset("raw").model_copy(update={"decode_status": "unsupported"})
    write_session(output, _config(output), "partial-session")
    (output / "assets.jsonl").write_text(
        ready.model_dump_json() + "\n" + unsupported.model_dump_json() + "\n",
        encoding="utf-8",
    )
    (output / "groups.json").write_text("[]\n", encoding="utf-8")
    items = [
        SelectionItem(
            item_id=f"item-{asset_id}",
            kind="capture",
            member_asset_ids=[asset_id],
            primary_asset_id=asset_id,
        )
        for asset_id in ("ready", "raw")
    ]
    (output / "selection-items.json").write_text(
        json.dumps([item.model_dump(mode="json") for item in items]) + "\n",
        encoding="utf-8",
    )

    assert main(["assess", str(output)]) == 3
    capsys.readouterr()
    assert main(["vision-request", str(output)]) == 0
    capsys.readouterr()
    request = json.loads((output / "vision-request.json").read_text(encoding="utf-8"))
    response_path = tmp_path / "partial-response.json"
    response_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "request_id": request["request_id"],
                "status": "completed",
                "observations": [
                    {
                        "asset_id": "ready",
                        "observed_artifact_ids": [request["artifacts"][0]["artifact_id"]],
                        "visual_criteria": {
                            name: "acceptable" for name in request["assets"][0]["criteria"]
                        },
                        "region_evidence": [],
                        "intent_hypotheses": [],
                        "uncertainty": [],
                    }
                ],
                "error": None,
            }
        ),
        encoding="utf-8",
    )

    assert main(["vision-import", str(output), str(response_path)]) == 3
    summary = json.loads(capsys.readouterr().out)
    report = json.loads((output / "assessment-report.json").read_text(encoding="utf-8"))
    assert summary["status"] == "partial"
    assert report["status"] == "partial"
    assert json.loads((output / "vision-status.json").read_text(encoding="utf-8"))[
        "status"
    ] == "partial"
