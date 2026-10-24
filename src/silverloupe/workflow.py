"""Persisted orchestration and revision-bound user corrections."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from silverloupe.contracts import (
    Decision,
    DecisionAuthority,
    RunConfig,
    RunState,
    SelectionItem,
    SelectionOverride,
    SelectionTier,
    SessionConfig,
)
from silverloupe.curation import curate_prepared
from silverloupe.grouping import group_prepared
from silverloupe.inventory import prepare
from silverloupe.previews import validate_output_layout
from silverloupe.state import atomic_write_text, invalidate_report_generation
from silverloupe.triage import assess_prepared, create_vision_request_prepared
from silverloupe.vision.host_exchange import NoVisionArtifactsError


ModelT = TypeVar("ModelT", bound=BaseModel)
_RUN_ARTIFACTS = (
    "session.json",
    "assets.jsonl",
    "groups.json",
    "selection-items.json",
    "assessments.jsonl",
    "decisions.jsonl",
    "assessment-report.json",
    "vision-request.json",
    "vision-status.json",
    "selection.json",
    "curation-report.json",
    "manual-overrides.jsonl",
    "override-generation.json",
    "run-state.json",
    "delivery-plan.json",
    "delivery-receipt.json",
    "delivery-rollback-receipt.json",
)


def _jsonl(values: list[BaseModel]) -> str:
    return "".join(value.model_dump_json() + "\n" for value in values)


def _read_json_array(path: Path, model: type[ModelT]) -> list[ModelT]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise FileNotFoundError(f"required artifact does not exist: {path}") from error
    except (OSError, ValueError) as error:
        raise ValueError(f"invalid {path.name}: {error}") from error
    if not isinstance(value, list):
        raise ValueError(f"invalid {path.name}: expected a JSON array")
    try:
        return [model.model_validate(item) for item in value]
    except ValidationError as error:
        raise ValueError(f"invalid {path.name}: {error}") from error


def _read_jsonl(path: Path, model: type[ModelT]) -> list[ModelT]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as error:
        raise FileNotFoundError(f"required artifact does not exist: {path}") from error
    result: list[ModelT] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            result.append(model.model_validate_json(line))
        except ValidationError as error:
            raise ValueError(f"invalid {path.name} line {line_number}: {error}") from error
    return result


def _session(output: Path) -> SessionConfig:
    try:
        return SessionConfig.model_validate_json(
            (output / "session.json").read_text(encoding="utf-8")
        )
    except FileNotFoundError as error:
        raise FileNotFoundError(f"session does not exist: {output / 'session.json'}") from error
    except (OSError, ValidationError) as error:
        raise ValueError(f"invalid session.json: {error}") from error


def _artifact_paths(output: Path) -> dict[str, str]:
    return {
        Path(name).stem.replace("-", "_"): name
        for name in _RUN_ARTIFACTS
        if (output / name).is_file()
    }


def _state(output: Path, session: SessionConfig, status: str, errors: list[str]) -> RunState:
    return RunState(
        session_id=session.session_id,
        revision=session.revision,
        status=status,
        output_root=str(output),
        artifact_paths=_artifact_paths(output),
        errors=errors,
    )


def _write_run_state(output: Path, state: RunState) -> None:
    paths = dict(state.artifact_paths)
    paths["run_state"] = "run-state.json"
    persisted = state.model_copy(update={"artifact_paths": paths})
    atomic_write_text(output / "run-state.json", persisted.model_dump_json(indent=2) + "\n")


def load_run_state(output: Path) -> RunState:
    """Load the last atomically published workflow state."""

    output = output.expanduser().resolve()
    try:
        state = RunState.model_validate_json(
            (output / "run-state.json").read_text(encoding="utf-8")
        )
    except FileNotFoundError as error:
        raise FileNotFoundError(f"run state does not exist: {output / 'run-state.json'}") from error
    except (OSError, ValidationError) as error:
        raise ValueError(f"invalid run-state.json: {error}") from error
    if Path(state.output_root).expanduser().resolve() != output:
        raise ValueError(
            "run-state output_root does not match the requested output directory"
        )
    return state


def run_session(config: RunConfig) -> RunState:
    """Run preparation through curation, stopping honestly for host evidence."""

    if len(config.source_roots) != 1:
        raise ValueError("run_session requires exactly one source root")
    source = Path(config.source_roots[0]).expanduser().resolve()
    output = Path(config.output_root).expanduser().resolve()
    normalized = config.model_copy(
        update={"source_roots": [str(source)], "output_root": str(output)}
    )
    validate_output_layout(output)
    prepare(source, output, normalized)
    group_prepared(output)
    assessment = assess_prepared(output)
    session = _session(output)
    if assessment["status"] != "completed":
        try:
            create_vision_request_prepared(output)
            errors = ["visual_evidence_required"]
        except NoVisionArtifactsError:
            errors = ["visual_evidence_unavailable"]
        state = _state(output, session, "partial", errors)
        _write_run_state(output, state)
        return load_run_state(output)
    curate_prepared(output)
    state = _state(output, session, "completed", [])
    _write_run_state(output, state)
    return load_run_state(output)


def _override_decision(decision: Decision, override: SelectionOverride) -> Decision:
    tier_by_action = {
        ("top", "include"): SelectionTier.TOP,
        ("top", "exclude"): SelectionTier.SELECT,
        ("selects", "include"): SelectionTier.SELECT,
        ("selects", "exclude"): SelectionTier.RESERVE,
    }
    tier = tier_by_action[(override.selection_scope, override.action)]
    rating = {
        SelectionTier.TOP: 5,
        SelectionTier.SELECT: 4,
        SelectionTier.RESERVE: 3,
    }[tier]
    return decision.model_copy(
        update={
            "tier": tier,
            "rating_proposal": rating,
            "reason_codes": list(dict.fromkeys([*decision.reason_codes, "user_override"])),
            "human_reason": override.reason,
            "provenance": {
                **decision.provenance,
                "override_revision": override.revision,
                "selection_scope": override.selection_scope,
                "action": override.action,
            },
            "decision_authority": DecisionAuthority.USER,
            "user_override": True,
        }
    )


def _validate_overrides(
    session: SessionConfig,
    items: list[SelectionItem],
    overrides: list[SelectionOverride],
) -> None:
    if not overrides:
        raise ValueError("at least one selection override is required")
    ids = [override.item_id for override in overrides]
    duplicates = sorted({item_id for item_id in ids if ids.count(item_id) > 1})
    if duplicates:
        raise ValueError(f"override item IDs must be unique: {duplicates}")
    known = {item.item_id for item in items}
    unknown = sorted(set(ids) - known)
    if unknown:
        raise ValueError(f"overrides reference unknown items: {unknown}")
    stale = sorted({override.revision for override in overrides if override.revision != session.revision})
    if stale:
        raise ValueError(
            f"override revision does not match active revision {session.revision}: {stale}"
        )


def _generation_status(output: Path, name: str) -> str | None:
    path = output / name
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"invalid {name}: {error}") from error
    if not isinstance(value, dict) or not isinstance(value.get("status"), str):
        raise ValueError(f"invalid {name}: missing status")
    return str(value["status"])


def _require_completed_run(output: Path) -> None:
    state = load_run_state(output)
    if state.status != "completed":
        raise ValueError("selection overrides require a completed run")
    if _generation_status(output, "curation-generation.json") != "complete":
        raise ValueError("selection overrides require a complete curation generation")
    override_status = _generation_status(output, "override-generation.json")
    if override_status not in {None, "complete"}:
        raise ValueError("override generation is incomplete; resolve it before applying changes")


def apply_overrides(output: Path, overrides: list[SelectionOverride]) -> RunState:
    """Validate all user corrections, publish them, and rerun curation."""

    output = output.expanduser().resolve()
    validate_output_layout(output)
    _require_completed_run(output)
    session = _session(output)
    items = _read_json_array(output / "selection-items.json", SelectionItem)
    _validate_overrides(session, items, overrides)
    decisions = _read_jsonl(output / "decisions.jsonl", Decision)
    decisions_by_id = {decision.item_id: decision for decision in decisions}
    missing_decisions = sorted({override.item_id for override in overrides} - set(decisions_by_id))
    if missing_decisions:
        raise ValueError(f"overrides lack current decisions: {missing_decisions}")

    override_by_id = {override.item_id: override for override in overrides}
    updated_decisions = [
        _override_decision(decision, override_by_id[decision.item_id])
        if decision.item_id in override_by_id
        else decision
        for decision in decisions
    ]
    manual_path = output / "manual-overrides.jsonl"
    prior_manual = manual_path.read_text(encoding="utf-8") if manual_path.is_file() else ""
    manual_text = prior_manual + _jsonl(overrides)
    generation_id = hashlib.sha256(manual_text.encode("utf-8")).hexdigest()
    marker_path = output / "override-generation.json"
    marker: dict[str, Any] = {
        "schema_version": 1,
        "generation_id": generation_id,
        "status": "publishing",
        "revision_before": session.revision,
        "files": [
            "manual-overrides.jsonl",
            "decisions.jsonl",
            "selection.json",
            "session.json",
            "run-state.json",
        ],
    }
    invalidate_report_generation(output, "override_changed")
    atomic_write_text(
        marker_path,
        json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    original_session = (output / "session.json").read_text(encoding="utf-8")
    try:
        atomic_write_text(manual_path, manual_text)
        atomic_write_text(output / "decisions.jsonl", _jsonl(updated_decisions))
        curate_prepared(output)
        updated_session = session.model_copy(
            update={
                "revision": session.revision + 1,
                "timestamps": {
                    **session.timestamps,
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            }
        )
        atomic_write_text(
            output / "session.json", updated_session.model_dump_json(indent=2) + "\n"
        )
        state = _state(output, updated_session, "completed", [])
        _write_run_state(output, state)
        marker.update(
            {"status": "complete", "revision_after": updated_session.revision}
        )
        atomic_write_text(
            marker_path,
            json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return load_run_state(output)
    except BaseException:
        atomic_write_text(output / "session.json", original_session)
        marker["status"] = "publishing"
        marker.pop("revision_after", None)
        atomic_write_text(
            marker_path,
            json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        raise
