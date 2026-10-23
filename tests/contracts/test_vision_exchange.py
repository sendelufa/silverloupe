from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from silverloupe.cli import main
from silverloupe.contracts import Assessment, Asset, Brief, Manifest, RunConfig, SelectionItem
from silverloupe.state import write_session
from silverloupe.vision.base import VisionObservation, VisionResponse
from silverloupe.vision.host_exchange import (
    apply_vision_response,
    build_vision_request,
    parse_vision_response,
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
                visual_criteria={"focus": "acceptable"},
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
                visual_criteria={"focus": "uncertain"},
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
                visual_criteria={"focus": "acceptable", "eye_state": "not_applicable"},
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
                        "visual_criteria": {"focus": "acceptable"},
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
