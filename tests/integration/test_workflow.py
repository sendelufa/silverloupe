from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

import silverloupe.workflow as workflow_module
from silverloupe.contracts import Brief, RunConfig, SelectionOverride
from silverloupe.triage import import_vision_response_prepared
from silverloupe.vision.base import VisionRequest, VisionResponse, VisionObservation
from silverloupe.workflow import apply_overrides, load_run_state, run_session


def _image(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 24), color).save(path, "JPEG")


def _hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
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
