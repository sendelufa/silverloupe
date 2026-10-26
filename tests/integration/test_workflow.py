from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

import silverloupe.report as report_module
import silverloupe.workflow as workflow_module
from silverloupe.cli import main
from silverloupe.contracts import Brief, RunConfig, SelectionOverride
from silverloupe.report import build_report
from silverloupe.triage import import_vision_response_prepared
from silverloupe.vision.base import VisionRequest, VisionResponse, VisionObservation
from silverloupe.workflow import apply_overrides, load_run_state, run_session
from tools.build_collection import build_collection


def _image(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 24), color).save(path, "JPEG")


def _hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _photo_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix.casefold() in {".arw", ".jpg", ".jpeg"}
    }


def _config(source: Path, output: Path) -> RunConfig:
    return RunConfig(
        source_roots=[str(source)],
        output_root=str(output),
        brief=Brief(genres=["architecture"], top_k=1),
        editor_config_snapshot={"vision_capability": "unavailable"},
    )


def _complete_vision(output: Path) -> None:
    request = VisionRequest.model_validate_json(
        (output / "vision-request.json").read_text(encoding="utf-8")
    )
    criteria_by_asset = {asset.asset_id: asset.criteria for asset in request.assets}
    response = VisionResponse(
        request_id=request.request_id,
        status="completed",
        observations=[
            VisionObservation(
                asset_id=asset.asset_id,
                observed_artifact_ids=asset.artifact_ids,
                visual_criteria={criterion: "acceptable" for criterion in criteria_by_asset[asset.asset_id]},
            )
            for asset in request.assets
        ],
    )
    response_path = output / "host-response.json"
    response_path.write_text(response.model_dump_json(indent=2) + "\n", encoding="utf-8")
    result = import_vision_response_prepared(output, response_path)
    assert result.status == "completed"


def _completed_run(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    output = tmp_path / "output"
    _image(source / "a.jpg", (20, 40, 60))
    _image(source / "b.jpg", (80, 100, 120))
    config = _config(source, output)
    assert run_session(config).status == "partial"
    _complete_vision(output)
    assert run_session(config).status == "completed"
    return source, output


def test_run_session_without_visual_evidence_returns_partial_and_preserves_sources(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    _image(source / "frame.jpg", (20, 40, 60))
    before = _hashes(source)

    state = run_session(_config(source, output))

    assert state.status == "partial"
    assert state.errors == ["visual_evidence_required"]
    assert _hashes(source) == before
    assert (output / "assets.jsonl").is_file()
    assert (output / "groups.json").is_file()
    assert (output / "assessments.jsonl").is_file()
    assert (output / "vision-request.json").is_file()
    assert (output / "run-state.json").is_file()
    assert not (output / "selection.json").exists()


def test_second_run_after_valid_visual_import_completes_curation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    _image(source / "frame.jpg", (20, 40, 60))
    config = _config(source, output)
    assert run_session(config).status == "partial"
    _complete_vision(output)

    state = run_session(config)

    assert state.status == "completed"
    selection = json.loads((output / "selection.json").read_text(encoding="utf-8"))
    assert selection["top_ids"]
    assert set(selection["top_ids"]).issubset(selection["selects_ids"])
    assert load_run_state(output) == state


def test_same_stem_unsupported_raw_uses_jpeg_evidence_and_completes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    _image(source / "IMG_0042.JPG", (20, 40, 60))
    (source / "IMG_0042.ARW").write_bytes(b"not-a-decodable-raw")
    config = _config(source, output)

    first = run_session(config)
    items = json.loads((output / "selection-items.json").read_text(encoding="utf-8"))

    assert first.status == "partial"
    assert len(items) == 1
    assert len(items[0]["member_asset_ids"]) == 2
    assets = [
        json.loads(line)
        for line in (output / "assets.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    raw_id = next(
        asset["asset_id"] for asset in assets if asset["relative_path"].endswith(".ARW")
    )
    assert items[0]["primary_asset_id"] == raw_id
    _complete_vision(output)

    state = run_session(config)
    report = json.loads((output / "assessment-report.json").read_text(encoding="utf-8"))

    assert state.status == "completed"
    assert (output / "selection.json").is_file()
    assert report["status"] == "completed"
    assert report["assessment_statuses"] == {"assessed": 1, "unassessed": 1}
    document = build_report(state).read_text(encoding="utf-8")
    assert "Preview unavailable" not in document
    assert "IMG_0042.ARW" in document


def test_run_state_paths_are_output_owned(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    _image(source / "frame.jpg", (20, 40, 60))

    state = run_session(_config(source, output))

    assert Path(state.output_root).is_absolute()
    for relative in state.artifact_paths.values():
        path = Path(relative)
        assert not path.is_absolute()
        assert ".." not in path.parts
        assert (output / path).resolve().is_relative_to(output.resolve())


def test_selection_override_is_strict_and_requires_a_reason() -> None:
    with pytest.raises(ValidationError):
        SelectionOverride(
            item_id="item-1",
            revision=1,
            selection_scope="top",
            action="exclude",
            reason="   ",
        )
    with pytest.raises(ValidationError):
        SelectionOverride.model_validate(
            {
                "item_id": "item-1",
                "revision": 1,
                "selection_scope": "top",
                "action": "exclude",
                "reason": "duplicate",
                "unexpected": True,
            }
        )


def test_apply_overrides_round_trip_preserves_user_authority_and_revision(
    tmp_path: Path,
) -> None:
    _source, output = _completed_run(tmp_path)
    session = json.loads((output / "session.json").read_text(encoding="utf-8"))
    selection = json.loads((output / "selection.json").read_text(encoding="utf-8"))
    first = selection["top_ids"][0]
    second = next(item for item in selection["ranked_ids"] if item != first)

    state = apply_overrides(
        output,
        [
            SelectionOverride(
                item_id=first,
                revision=session["revision"],
                selection_scope="top",
                action="exclude",
                reason="prefer the alternate expression",
            ),
            SelectionOverride(
                item_id=second,
                revision=session["revision"],
                selection_scope="top",
                action="include",
                reason="client preference",
            ),
        ],
    )

    updated = json.loads((output / "selection.json").read_text(encoding="utf-8"))
    decisions = [json.loads(line) for line in (output / "decisions.jsonl").read_text().splitlines()]
    decisions_by_id = {decision["item_id"]: decision for decision in decisions}
    assert state.status == "completed"
    assert first in updated["selects_ids"] and first not in updated["top_ids"]
    assert second in updated["top_ids"]
    assert set(updated["top_ids"]).issubset(updated["selects_ids"])
    assert decisions_by_id[first]["decision_authority"] == "user"
    assert decisions_by_id[second]["user_override"] is True
    assert json.loads((output / "session.json").read_text())["revision"] == session["revision"] + 1
    assert json.loads((output / "override-generation.json").read_text())["status"] == "complete"
    assert len((output / "manual-overrides.jsonl").read_text().splitlines()) == 2

    rerun = run_session(_config(Path(session["source_roots"][0]), output))
    rerun_decisions = [json.loads(line) for line in (output / "decisions.jsonl").read_text().splitlines()]
    assert rerun.status == "completed"
    assert {d["item_id"] for d in rerun_decisions if d["decision_authority"] == "user"} == {
        first,
        second,
    }


@pytest.mark.parametrize("kind", ["unknown", "duplicate", "stale"])
def test_invalid_overrides_do_not_mutate_owned_state(tmp_path: Path, kind: str) -> None:
    _source, output = _completed_run(tmp_path)
    session = json.loads((output / "session.json").read_text())
    selection = json.loads((output / "selection.json").read_text())
    item_id = selection["ranked_ids"][0]
    valid = SelectionOverride(
        item_id=item_id,
        revision=session["revision"],
        selection_scope="selects",
        action="exclude",
        reason="not needed",
    )
    if kind == "unknown":
        overrides = [valid.model_copy(update={"item_id": "item-missing"})]
    elif kind == "duplicate":
        overrides = [valid, valid.model_copy(update={"reason": "same item twice"})]
    else:
        overrides = [valid.model_copy(update={"revision": session["revision"] - 1})]
    before = _hashes(output)

    with pytest.raises(ValueError):
        apply_overrides(output, overrides)

    assert _hashes(output) == before


@pytest.mark.parametrize(
    "failed_name",
    ["manual-overrides.jsonl", "decisions.jsonl", "session.json", "run-state.json"],
)
def test_interrupted_override_generation_never_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_name: str
) -> None:
    _source, output = _completed_run(tmp_path)
    session = json.loads((output / "session.json").read_text())
    selection = json.loads((output / "selection.json").read_text())
    override = SelectionOverride(
        item_id=selection["ranked_ids"][0],
        revision=session["revision"],
        selection_scope="selects",
        action="exclude",
        reason="failure injection",
    )
    real_atomic_write = workflow_module.atomic_write_text
    failed = False

    def interrupt(path: Path, text: str) -> None:
        nonlocal failed
        if path.name == failed_name and not failed:
            failed = True
            raise OSError(f"simulated interruption at {failed_name}")
        real_atomic_write(path, text)

    monkeypatch.setattr(workflow_module, "atomic_write_text", interrupt)
    with pytest.raises(OSError, match="simulated interruption"):
        apply_overrides(output, [override])

    marker = json.loads((output / "override-generation.json").read_text())
    assert marker["status"] == "publishing"
    assert json.loads((output / "session.json").read_text())["revision"] == session["revision"]

    with pytest.raises(ValueError, match="override generation is incomplete"):
        build_report(load_run_state(output))


def test_partial_run_rejects_overrides_without_mutation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    _image(source / "frame.jpg", (20, 40, 60))
    state = run_session(_config(source, output))
    session = json.loads((output / "session.json").read_text())
    item = json.loads((output / "selection-items.json").read_text())[0]
    override = SelectionOverride(
        item_id=item["item_id"],
        revision=session["revision"],
        selection_scope="top",
        action="include",
        reason="manual preference",
    )
    before = _hashes(output)

    with pytest.raises(ValueError, match="completed run"):
        apply_overrides(output, [override])

    assert state.status == "partial"
    assert _hashes(output) == before


@pytest.mark.parametrize("status", ["review", "unassessed", "reject"])
def test_tier_override_preserves_existing_decision_status(status: str) -> None:
    decision = workflow_module.Decision(
        item_id="item-1",
        status=status,
        tier="select",
        decision_authority="user" if status == "reject" else "proposal",
    )
    override = SelectionOverride(
        item_id="item-1",
        revision=1,
        selection_scope="selects",
        action="exclude",
        reason="move out of selects",
    )

    updated = workflow_module._override_decision(decision, override)

    assert str(updated.status) == status
    assert str(updated.tier) == "reserve"


def test_partial_report_discloses_missing_visual_evidence(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    _image(source / "frame.jpg", (20, 40, 60))
    state = run_session(_config(source, output))

    report_path = build_report(state)
    document = report_path.read_text(encoding="utf-8")

    assert report_path == source / "report.html"
    assert (source / ".silverloupe-report/previews.css").is_file()
    assert not (output / "report/index.html").exists()
    assert "Partial" in document
    assert "visual evidence" in document.lower()
    assert "vision-request.json" in document
    assert "Real-photo quality metrics: unavailable" in document
    assert "<form" not in document.lower()
    assert "<script" not in document.lower()


def test_report_survives_output_deletion_without_importable_preview_files(
    tmp_path: Path,
) -> None:
    source, output = _completed_run(tmp_path)

    report_path = build_report(load_run_state(output))
    assets_path = source / ".silverloupe-report/previews.css"
    document = report_path.read_text(encoding="utf-8")
    stylesheet = assets_path.read_text(encoding="utf-8")

    assert '.silverloupe-report/previews.css' in document
    assert "data:image/jpeg;base64," in stylesheet
    assert not list((source / ".silverloupe-report").rglob("*.jpg"))
    assert not list((source / ".silverloupe-report").rglob("*.jpeg"))

    shutil.rmtree(output)

    assert report_path.is_file()
    assert assets_path.is_file()
    assert "data:image/jpeg;base64," in assets_path.read_text(encoding="utf-8")


def test_report_refuses_to_overwrite_foreign_source_report(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    _image(source / "frame.jpg", (20, 40, 60))
    foreign = source / "report.html"
    foreign.write_text("personal report", encoding="utf-8")
    state = run_session(_config(source, output))

    with pytest.raises(ValueError, match="not owned by Silverloupe"):
        build_report(state)

    assert foreign.read_text(encoding="utf-8") == "personal report"
    assert not (source / ".silverloupe-report").exists()


@pytest.mark.parametrize("fixture", ["empty", "corrupt"])
def test_run_without_requestable_previews_publishes_useful_partial_report(
    tmp_path: Path, fixture: str
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    if fixture == "corrupt":
        (source / "broken.jpg").write_bytes(b"not-a-jpeg")

    state = run_session(_config(source, output))
    document = build_report(state).read_text(encoding="utf-8")

    assert state.status == "partial"
    assert state.errors == ["visual_evidence_unavailable"]
    assert (output / "run-state.json").is_file()
    assert not (output / "vision-request.json").exists()
    assert "Visual evidence unavailable" in document
    assert "Partial" in document


def test_report_cards_keep_details_readable_in_multi_card_sections(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    _image(source / "a.jpg", (20, 40, 60))
    _image(source / "b.jpg", (80, 100, 120))

    document = build_report(run_session(_config(source, output))).read_text(encoding="utf-8")

    assert "repeat(auto-fit, minmax(260px, 1fr))" in document
    assert "dl { display: grid; grid-template-columns: minmax(0, 1fr)" in document


def test_complete_report_shows_all_sections_and_provenance(tmp_path: Path) -> None:
    _source, output = _completed_run(tmp_path)

    document = build_report(load_run_state(output)).read_text(encoding="utf-8")

    for heading in ("Top", "Selects", "Review", "Reject", "Reserve"):
        assert f">{heading}<" in document
    assert "reason" in document.lower()
    assert "comparative_score" in document
    assert "alternative" in document.lower()
    assert "profile" in document.lower()
    assert "Coverage" in document
    assert "Constraints" in document
    assert "Real-photo quality metrics: unavailable" in document


def test_partial_rerun_invalidates_report_and_hides_stale_selection(tmp_path: Path) -> None:
    source, output = _completed_run(tmp_path)
    prior_state = load_run_state(output)
    build_report(prior_state)
    assert json.loads((output / "report-generation.json").read_text())["status"] == "complete"
    _image(source / "a.jpg", (200, 10, 10))

    state = run_session(_config(source, output))

    assert state.status == "partial"
    assert json.loads((output / "report-generation.json").read_text())["status"] == "invalidated"
    document = build_report(state).read_text(encoding="utf-8")
    assert '<h2>Top</h2><div class="grid"><p class="empty">' in document
    assert '<h2>Selects</h2><div class="grid"><p class="empty">' in document


def test_report_rejects_session_revision_newer_than_run_state(tmp_path: Path) -> None:
    _source, output = _completed_run(tmp_path)
    state = load_run_state(output)
    session_path = output / "session.json"
    session = json.loads(session_path.read_text())
    session["revision"] += 1
    session_path.write_text(json.dumps(session), encoding="utf-8")

    with pytest.raises(ValueError, match="revision"):
        build_report(state)


def test_load_run_state_rejects_relocated_output_root(tmp_path: Path) -> None:
    _source, output = _completed_run(tmp_path)
    copied = tmp_path / "copied-output"
    shutil.copytree(output, copied)

    with pytest.raises(ValueError, match="output_root does not match"):
        load_run_state(copied)


def test_report_escapes_untrusted_text_and_uses_only_output_relative_previews(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    _image(source / "<script>alert(1)</script>.jpg", (20, 40, 60))
    config = _config(source, output)
    assert run_session(config).status == "partial"
    _complete_vision(output)
    state = run_session(config)
    decisions_path = output / "decisions.jsonl"
    decisions = [json.loads(line) for line in decisions_path.read_text().splitlines()]
    decisions[0]["human_reason"] = "<script>alert('reason')</script>"
    decisions[0]["provenance"]["alternative_ids"] = ["<img src=x onerror=alert(1)>"]
    decisions_path.write_text(
        "".join(json.dumps(value) + "\n" for value in decisions), encoding="utf-8"
    )

    document = build_report(state).read_text(encoding="utf-8")

    assert "<script>alert" not in document
    assert "&lt;script&gt;alert" in document
    assert "<img src=x" not in document
    assert ".silverloupe-report/previews.css" in document
    assert str(output) not in document


@pytest.mark.parametrize("boundary", ["stylesheet", "report", "complete-marker"])
def test_interrupted_report_generation_stays_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    _image(source / "frame.jpg", (20, 40, 60))
    state = run_session(_config(source, output))
    real_atomic_write = report_module.atomic_write_text
    marker_writes = 0

    def interrupt(path: Path, text: str) -> None:
        nonlocal marker_writes
        if path.name == "report-generation.json":
            marker_writes += 1
        if boundary == "stylesheet" and path.name == "previews.css":
            raise OSError("simulated report interruption")
        if boundary == "report" and path.name == "report.html":
            raise OSError("simulated report interruption")
        if boundary == "complete-marker" and path.name == "report-generation.json" and marker_writes == 2:
            raise OSError("simulated report interruption")
        real_atomic_write(path, text)

    monkeypatch.setattr(report_module, "atomic_write_text", interrupt)
    with pytest.raises(OSError, match="simulated report interruption"):
        build_report(state)

    marker = json.loads((output / "report-generation.json").read_text())
    assert marker["status"] == "publishing"


def test_cli_run_report_and_apply_overrides_from_another_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "фото source"
    output = tmp_path / "результат output"
    elsewhere = tmp_path / "other cwd"
    elsewhere.mkdir()
    _image(source / "a.jpg", (20, 40, 60))
    _image(source / "b.jpg", (80, 100, 120))
    brief_path = tmp_path / "brief.json"
    brief_path.write_text(Brief(genres=["architecture"], top_k=1).model_dump_json(), encoding="utf-8")
    monkeypatch.chdir(elsewhere)

    assert main(["run", str(source), str(output), "--brief", str(brief_path)]) == 3
    run_summary = json.loads(capsys.readouterr().out)
    assert run_summary["status"] == "partial"
    assert Path(run_summary["report"]).is_file()
    _complete_vision(output)
    assert main(["run", str(source), str(output), "--brief", str(brief_path)]) == 0
    complete_summary = json.loads(capsys.readouterr().out)
    assert complete_summary["status"] == "completed"

    assert main(["report", str(output)]) == 0
    report_summary = json.loads(capsys.readouterr().out)
    assert report_summary["status"] == "completed"
    session = json.loads((output / "session.json").read_text())
    selection = json.loads((output / "selection.json").read_text())
    overrides_path = tmp_path / "правки.json"
    overrides_path.write_text(
        json.dumps(
            [
                {
                    "schema_version": 1,
                    "item_id": selection["top_ids"][0],
                    "revision": session["revision"],
                    "selection_scope": "top",
                    "action": "exclude",
                    "reason": "client requested the alternate",
                }
            ]
        ),
        encoding="utf-8",
    )

    assert main(["apply-overrides", str(output), str(overrides_path)]) == 0
    override_summary = json.loads(capsys.readouterr().out)
    assert override_summary["status"] == "completed"
    assert override_summary["revision"] == session["revision"] + 1
    assert Path(override_summary["report"]).is_file()


def test_cli_run_rejects_invalid_brief_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source"
    _image(source / "a.jpg", (20, 40, 60))
    brief = tmp_path / "brief.json"
    brief.write_text("{not-json", encoding="utf-8")

    assert main(["run", str(source), str(tmp_path / "output"), "--brief", str(brief)]) == 2
    assert "invalid brief" in capsys.readouterr().err.lower()


def test_cli_run_reports_io_failure_as_exit_four(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    brief = tmp_path / "brief.json"
    brief.write_text(Brief().model_dump_json(), encoding="utf-8")

    def fail_io(_config: RunConfig):
        raise OSError("disk unavailable")

    monkeypatch.setattr("silverloupe.cli.run_session", fail_io)
    assert main(["run", str(tmp_path / "source"), str(tmp_path / "output"), "--brief", str(brief)]) == 4
    assert "disk unavailable" in capsys.readouterr().err


def test_full_cli_round_trip_and_built_runtime_are_source_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "input photos"
    output = tmp_path / "review output"
    clean_cwd = tmp_path / "clean host cwd"
    clean_cwd.mkdir()
    _image(source / "one.jpg", (20, 40, 60))
    _image(source / "two.jpg", (80, 100, 120))
    before = _photo_hashes(source)
    brief = tmp_path / "brief.json"
    brief.write_text(Brief(genres=["architecture"], top_k=1).model_dump_json(), encoding="utf-8")
    runtime = tmp_path / "installed runtime"
    build_collection(Path(__file__).parents[2], runtime, kind="runtime")
    monkeypatch.chdir(clean_cwd)

    assert main(["run", str(source), str(output), "--brief", str(brief)]) == 3
    partial = json.loads(capsys.readouterr().out)
    assert Path(partial["report"]).is_file()
    assert not (output / "selection.json").exists()

    request = VisionRequest.model_validate_json((output / "vision-request.json").read_text())
    response = VisionResponse(
        request_id=request.request_id,
        status="completed",
        observations=[
            VisionObservation(
                asset_id=asset.asset_id,
                observed_artifact_ids=asset.artifact_ids,
                visual_criteria={criterion: "acceptable" for criterion in asset.criteria},
            )
            for asset in request.assets
        ],
    )
    response_path = tmp_path / "vision response.json"
    response_path.write_text(response.model_dump_json(), encoding="utf-8")
    assert main(["vision-import", str(output), str(response_path)]) == 0
    capsys.readouterr()
    assert main(["run", str(source), str(output), "--brief", str(brief)]) == 0
    capsys.readouterr()

    session = json.loads((output / "session.json").read_text())
    selection = json.loads((output / "selection.json").read_text())
    corrected_id = selection["top_ids"][0]
    overrides = tmp_path / "overrides.json"
    overrides.write_text(
        json.dumps(
            [
                {
                    "schema_version": 1,
                    "item_id": corrected_id,
                    "revision": session["revision"],
                    "selection_scope": "top",
                    "action": "exclude",
                    "reason": "manual E2E correction",
                }
            ]
        ),
        encoding="utf-8",
    )
    assert main(["apply-overrides", str(output), str(overrides)]) == 0
    corrected = json.loads(capsys.readouterr().out)
    assert corrected["revision"] == session["revision"] + 1
    assert main(["run", str(source), str(output), "--brief", str(brief)]) == 0
    capsys.readouterr()

    decisions = [json.loads(line) for line in (output / "decisions.jsonl").read_text().splitlines()]
    corrected_decision = next(value for value in decisions if value["item_id"] == corrected_id)
    assert corrected_decision["decision_authority"] == "user"
    assert corrected_decision["tier"] == "select"
    assert _photo_hashes(source) == before
    assert (source / "report.html").is_file()
    assert (source / ".silverloupe-report/previews.css").is_file()
    assert not any(part in {"evals", "tests", "docs"} for path in runtime.rglob("*") for part in path.parts)
    assert all(str(Path(__file__).parents[2]) not in path.read_text(encoding="utf-8") for path in runtime.rglob("*.md"))
