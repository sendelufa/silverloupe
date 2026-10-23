from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageFilter

from silverloupe.cli import main
from silverloupe.contracts import (
    Assessment,
    Asset,
    Brief,
    DecisionStatus,
    Manifest,
    RunConfig,
    SelectionItem,
)
from silverloupe.state import write_session
from silverloupe import triage
from silverloupe.triage import assess, propose_triage


def _asset(
    asset_id: str,
    *,
    preview: str | None = None,
    decode_status: str = "ready",
) -> Asset:
    return Asset(
        asset_id=asset_id,
        relative_path=f"{asset_id}.jpg",
        content_sha256=hashlib.sha256(asset_id.encode()).hexdigest(),
        media_type="image/jpeg",
        decode_status=decode_status,
        preview_provenance={"status": "ready", "path": preview} if preview else None,
    )


def _config(output: Path, *, vision: str = "unavailable") -> RunConfig:
    return RunConfig(
        source_roots=[str(output / "source")],
        output_root=str(output),
        brief=Brief(),
        rendering_policy={},
        budget={},
        editor_config_snapshot={"vision_capability": vision},
    )


def _item(asset_id: str, *, item_id: str | None = None) -> SelectionItem:
    return SelectionItem(
        item_id=item_id or f"item-{asset_id}",
        kind="capture",
        member_asset_ids=[asset_id],
        primary_asset_id=asset_id,
    )


def _save_preview(output: Path, name: str, image: Image.Image) -> str:
    relative = f"previews/{name}.jpg"
    path = output / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "JPEG")
    return relative


def test_assess_records_numeric_signals_without_claiming_visual_evaluation(tmp_path: Path) -> None:
    preview = _save_preview(tmp_path, "normal", Image.new("L", (800, 600), 128))
    manifest = Manifest(assets=[_asset("normal", preview=preview)])

    result = assess(manifest, [], Brief(), _config(tmp_path))

    assert len(result) == 1
    assert result[0].evaluation_status == "assessed"
    assert result[0].visual_status == "not_evaluated"
    assert result[0].observed_artifacts == []
    assert result[0].visual_criteria == {}
    assert result[0].technical_signals["preview"]["width"] == 800
    assert "luminance_mean" in result[0].technical_signals
    assert "laplacian_variance" in result[0].technical_signals


def test_unsupported_asset_is_unassessed_and_never_rejected(tmp_path: Path) -> None:
    manifest = Manifest(assets=[_asset("raw", decode_status="unsupported")])
    assessments = assess(manifest, [], Brief(), _config(tmp_path))

    decisions = propose_triage(assessments, [_item("raw")], Brief())

    assert assessments[0].evaluation_status == "unassessed"
    assert "decode_unavailable" in assessments[0].uncertainty
    assert decisions[0].status in {DecisionStatus.REVIEW, DecisionStatus.UNASSESSED}
    assert decisions[0].status is not DecisionStatus.REJECT
    assert decisions[0].provenance["auto_reject_enabled"] is False


def test_small_preview_requires_review_instead_of_false_reject(tmp_path: Path) -> None:
    preview = _save_preview(tmp_path, "small", Image.new("L", (120, 80), 128))
    manifest = Manifest(assets=[_asset("small", preview=preview)])
    assessments = assess(manifest, [], Brief(), _config(tmp_path))

    decision = propose_triage(assessments, [_item("small")], Brief())[0]

    assert "preview_too_small" in assessments[0].uncertainty
    assert decision.status == DecisionStatus.REVIEW


def test_low_key_noise_and_possible_intentional_blur_stay_reviewable(tmp_path: Path) -> None:
    low_key = Image.new("L", (800, 600), 5)
    rng = np.random.default_rng(42)
    noisy = Image.fromarray(rng.integers(0, 256, (600, 800), dtype=np.uint8), mode="L")
    sharp = Image.new("L", (800, 600), 0)
    sharp.paste(255, (200, 150, 600, 450))
    blurred = sharp.filter(ImageFilter.GaussianBlur(radius=24))
    previews = {
        "low": _save_preview(tmp_path, "low", low_key),
        "noise": _save_preview(tmp_path, "noise", noisy),
        "blur": _save_preview(tmp_path, "blur", blurred),
    }
    manifest = Manifest(
        assets=[_asset(asset_id, preview=preview) for asset_id, preview in previews.items()]
    )
    brief = Brief(allowed_artistic_techniques=["motion_blur", "low_key", "grain"])

    assessments = assess(manifest, [], brief, _config(tmp_path))
    decisions = propose_triage(
        assessments, [_item(asset.asset_id) for asset in manifest.assets], brief
    )

    assert all(decision.status == DecisionStatus.REVIEW for decision in decisions)
    assert not [decision for decision in decisions if decision.status == DecisionStatus.REJECT]
    assert all("artistic_intent_possible" in assessment.uncertainty for assessment in assessments)


def test_unique_moment_with_technical_uncertainty_is_forced_to_review(tmp_path: Path) -> None:
    preview = _save_preview(tmp_path, "moment", Image.new("L", (800, 600), 128))
    manifest = Manifest(assets=[_asset("moment", preview=preview)])
    item = _item("moment", item_id="unique-moment")
    brief = Brief(preselected_item_ids=[item.item_id])

    assessment = assess(manifest, [], brief, _config(tmp_path))[0]
    decision = propose_triage([assessment], [item], brief)[0]

    assert decision.status == DecisionStatus.REVIEW
    assert "unique_moment" in decision.reason_codes


def test_closed_eyes_during_kiss_are_contextual_review_not_reject() -> None:
    assessment = Assessment(
        asset_id="kiss",
        evaluation_status="assessed",
        technical_signals={"decode_status": "ready"},
        visual_status="evaluated",
        visual_criteria={"eye_state": "concern"},
        observed_artifacts=["overview-kiss"],
        intent_hypotheses=["kiss_or_embrace"],
        uncertainty=["closed_eyes_context_dependent"],
    )

    decision = propose_triage([assessment], [_item("kiss")], Brief(genres=["wedding"]))[0]

    assert decision.status == DecisionStatus.REVIEW
    assert decision.status is not DecisionStatus.REJECT
    assert "closed_eyes_context_dependent" in decision.reason_codes


def test_cli_assess_writes_auditable_partial_artifacts_from_another_cwd(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    output = tmp_path / "output"
    elsewhere = tmp_path / "elsewhere"
    output.mkdir()
    elsewhere.mkdir()
    preview = _save_preview(output, "cli", Image.new("L", (800, 600), 128))
    asset = _asset("cli", preview=preview)
    write_session(output, _config(output), "triage-session")
    (output / "assets.jsonl").write_text(asset.model_dump_json() + "\n", encoding="utf-8")
    (output / "groups.json").write_text("[]\n", encoding="utf-8")
    (output / "selection-items.json").write_text(
        json.dumps([_item("cli").model_dump(mode="json")]) + "\n", encoding="utf-8"
    )
    monkeypatch.chdir(elsewhere)

    assert main(["assess", str(output)]) == 3
    summary = json.loads(capsys.readouterr().out)

    assert summary["status"] == "partial"
    assert summary["auto_reject_enabled"] is False
    assessment = Assessment.model_validate_json(
        (output / "assessments.jsonl").read_text(encoding="utf-8").strip()
    )
    assert assessment.visual_status == "not_evaluated"
    assert (output / "decisions.jsonl").is_file()
    report = json.loads((output / "assessment-report.json").read_text(encoding="utf-8"))
    assert report["vision"] == "unavailable"


@pytest.mark.parametrize("failing_write", [2, 3, 4, 5])
def test_interrupted_assessment_publication_is_marked_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_write: int
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    preview = _save_preview(output, "publish", Image.new("L", (800, 600), 128))
    asset = _asset("publish", preview=preview)
    write_session(output, _config(output), "publish-session")
    (output / "assets.jsonl").write_text(asset.model_dump_json() + "\n", encoding="utf-8")
    (output / "groups.json").write_text("[]\n", encoding="utf-8")
    (output / "selection-items.json").write_text(
        json.dumps([_item("publish").model_dump(mode="json")]) + "\n", encoding="utf-8"
    )
    triage.assess_prepared(output)
    original_write = triage.atomic_write_text
    calls = 0

    def fail_during_publication(path: Path, contents: str) -> None:
        nonlocal calls
        calls += 1
        if calls == failing_write:
            raise OSError("simulated disk failure")
        original_write(path, contents)

    monkeypatch.setattr(triage, "atomic_write_text", fail_during_publication)

    with pytest.raises(OSError, match="simulated disk failure"):
        triage.assess_prepared(output)

    marker = json.loads((output / "assessment-generation.json").read_text(encoding="utf-8"))
    assert marker["status"] == "publishing"
    with pytest.raises(ValueError, match="assessment generation is incomplete"):
        triage.create_vision_request_prepared(output)


def test_reassessment_invalidates_complete_curation_before_republishing(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    preview = _save_preview(output, "rerun", Image.new("L", (800, 600), 128))
    asset = _asset("rerun", preview=preview)
    write_session(output, _config(output), "rerun-session")
    (output / "assets.jsonl").write_text(asset.model_dump_json() + "\n", encoding="utf-8")
    (output / "groups.json").write_text("[]\n", encoding="utf-8")
    (output / "selection-items.json").write_text(
        json.dumps([_item("rerun").model_dump(mode="json")]) + "\n",
        encoding="utf-8",
    )
    triage.assess_prepared(output)
    (output / "curation-generation.json").write_text(
        json.dumps({"schema_version": 1, "status": "complete"}) + "\n",
        encoding="utf-8",
    )

    triage.assess_prepared(output)

    marker = json.loads((output / "curation-generation.json").read_text(encoding="utf-8"))
    assert marker["status"] == "invalidated"
    assert marker["reason"] == "assessment_changed"
