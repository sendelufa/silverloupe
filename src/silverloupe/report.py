"""Escaped, static HTML review report publication."""

from __future__ import annotations

import base64
import hashlib
import json
from html import escape
from importlib.resources import files
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from silverloupe.contracts import (
    Assessment,
    Asset,
    Decision,
    RunState,
    Selection,
    SelectionItem,
    SessionConfig,
)
from silverloupe.previews import validate_output_layout
from silverloupe.state import atomic_write_text


ModelT = TypeVar("ModelT", bound=BaseModel)


def _read_json(path: Path, model: type[ModelT]) -> ModelT:
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise FileNotFoundError(f"required report artifact does not exist: {path}") from error
    except (OSError, ValidationError) as error:
        raise ValueError(f"invalid {path.name}: {error}") from error


def _read_array(path: Path, model: type[ModelT]) -> list[ModelT]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise FileNotFoundError(f"required report artifact does not exist: {path}") from error
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
        raise FileNotFoundError(f"required report artifact does not exist: {path}") from error
    result: list[ModelT] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            result.append(model.model_validate_json(line))
        except ValidationError as error:
            raise ValueError(f"invalid {path.name} line {line_number}: {error}") from error
    return result


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


def _validate_snapshot(state: RunState, output: Path, session: SessionConfig) -> None:
    persisted = _read_json(output / "run-state.json", RunState)
    if persisted != state:
        raise ValueError("run state is stale; reload it before building the report")
    if Path(persisted.output_root).expanduser().resolve() != output:
        raise ValueError("run-state output_root does not match the report output directory")
    if session.session_id != state.session_id:
        raise ValueError("run state does not match the active session")
    if session.revision != state.revision:
        raise ValueError("run-state revision does not match the active session revision")
    if _generation_status(output, "assessment-generation.json") != "complete":
        raise ValueError("assessment generation is incomplete; run assess again")
    override_status = _generation_status(output, "override-generation.json")
    if override_status not in {None, "complete"}:
        raise ValueError("override generation is incomplete; resolve it before reporting")
    if (
        state.status == "completed"
        and _generation_status(output, "curation-generation.json") != "complete"
    ):
        raise ValueError("curation generation is incomplete; run curate again")


def _safe_preview(output: Path, asset: Asset | None) -> Path | None:
    if asset is None or not asset.preview_provenance:
        return None
    relative = asset.preview_provenance.get("path")
    if not isinstance(relative, str):
        return None
    value = Path(relative)
    if value.is_absolute() or ".." in value.parts:
        return None
    candidate = output / value
    if candidate.is_symlink() or not candidate.resolve().is_relative_to(output.resolve()):
        return None
    if not candidate.is_file():
        return None
    return candidate


def _text(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _card(
    output: Path,
    item: SelectionItem,
    decision: Decision,
    assets: dict[str, Asset],
    assessments: dict[str, Assessment],
    preview_rules: dict[str, str],
) -> str:
    primary = assets.get(item.primary_asset_id)
    filename = primary.relative_path if primary is not None else item.primary_asset_id
    preview = _safe_preview(output, primary)
    evidence_asset = primary
    if preview is None:
        for asset_id in item.member_asset_ids:
            candidate = assets.get(asset_id)
            candidate_preview = _safe_preview(output, candidate)
            if candidate_preview is not None:
                evidence_asset = candidate
                preview = candidate_preview
                break
    if preview is not None:
        preview_class = "slp-preview-" + hashlib.sha256(
            evidence_asset.asset_id.encode("utf-8")
        ).hexdigest()[:20]
        encoded = base64.b64encode(preview.read_bytes()).decode("ascii")
        preview_rules[preview_class] = (
            f'.{preview_class}{{background-image:url("data:image/jpeg;base64,{encoded}")}}'
        )
        image = (
            f'<div class="preview {preview_class}" role="img" '
            f'aria-label="Preview of {escape(filename, quote=True)}"></div>'
        )
    else:
        image = '<p class="empty">Preview unavailable</p>'
    assessment = assessments.get(
        evidence_asset.asset_id if evidence_asset is not None else item.primary_asset_id
    )
    rows = [
        ("File", filename),
        (
            "Visual evidence file",
            evidence_asset.relative_path if evidence_asset is not None else "unavailable",
        ),
        ("Item", item.item_id),
        ("Status", decision.status),
        ("Tier", decision.tier),
        ("Reason codes", decision.reason_codes),
        ("Reason", decision.human_reason),
        ("Authority", decision.decision_authority),
        ("Visual status", assessment.visual_status if assessment else "unavailable"),
        ("Required assets", item.required_asset_ids),
        ("Comparisons / alternatives", decision.provenance.get("alternative_ids", [])),
        ("Score provenance", decision.provenance),
    ]
    details = "".join(
        f"<dt>{escape(label)}</dt><dd>{escape(_text(value))}</dd>" for label, value in rows
    )
    return f"<article>{image}<h3>{escape(filename)}</h3><dl>{details}</dl></article>"


def _section(
    heading: str,
    item_ids: list[str],
    output: Path,
    items: dict[str, SelectionItem],
    decisions: dict[str, Decision],
    assets: dict[str, Asset],
    assessments: dict[str, Assessment],
    preview_rules: dict[str, str],
) -> str:
    cards = [
        _card(
            output,
            items[item_id],
            decisions[item_id],
            assets,
            assessments,
            preview_rules,
        )
        for item_id in item_ids
        if item_id in items and item_id in decisions
    ]
    content = "".join(cards) if cards else '<p class="empty">No items in this section.</p>'
    return f"<section><h2>{escape(heading)}</h2><div class=\"grid\">{content}</div></section>"


def _render(state: RunState) -> tuple[str, str]:
    output = Path(state.output_root).resolve()
    session = _read_json(output / "session.json", SessionConfig)
    _validate_snapshot(state, output, session)
    assets = {value.asset_id: value for value in _read_jsonl(output / "assets.jsonl", Asset)}
    items = {value.item_id: value for value in _read_array(output / "selection-items.json", SelectionItem)}
    assessments = {
        value.asset_id: value for value in _read_jsonl(output / "assessments.jsonl", Assessment)
    }
    decisions = {value.item_id: value for value in _read_jsonl(output / "decisions.jsonl", Decision)}
    selection = (
        _read_json(output / "selection.json", Selection)
        if state.status == "completed" and (output / "selection.json").is_file()
        else Selection()
    )

    top_ids = list(selection.top_ids)
    select_ids = [item_id for item_id in selection.selects_ids if item_id not in set(top_ids)]
    review_ids = [item_id for item_id, value in decisions.items() if str(value.status) == "review"]
    reject_ids = [item_id for item_id, value in decisions.items() if str(value.status) == "reject"]
    reserve_ids = [
        item_id
        for item_id, value in decisions.items()
        if str(value.tier) in {"reserve", "none"}
        and item_id not in set(review_ids)
        and item_id not in set(reject_ids)
    ]
    preview_rules: dict[str, str] = {}
    sections = "".join(
        _section(
            name,
            ids,
            output,
            items,
            decisions,
            assets,
            assessments,
            preview_rules,
        )
        for name, ids in (
            ("Top", top_ids),
            ("Selects", select_ids),
            ("Review", review_ids),
            ("Reject", reject_ids),
            ("Reserve", reserve_ids),
        )
    )
    status_label = state.status.title()
    summary = (
        f'<p><span class="status">{escape(status_label)}</span> '
        f'Session {escape(session.session_id)}, revision {session.revision}</p>'
    )
    if "visual_evidence_unavailable" in state.errors:
        disclosure = (
            '<section class="disclosure"><h2>Visual evidence unavailable</h2>'
            '<p>The run is partial because no safe preview artifact can be sent to host vision. '
            'Review the available inventory and decode limitations; do not treat this result as '
            'a completed selection.</p></section>'
        )
    elif state.status == "partial":
        disclosure = (
            '<section class="disclosure"><h2>Visual evidence required</h2>'
            '<p>The run is partial. Review <code>vision-request.json</code> and import '
            'host visual evidence before treating the selection as complete.</p></section>'
        )
    else:
        disclosure = (
            '<section class="disclosure"><h2>Recommendation-only result</h2>'
            '<p>Selections remain proposals unless decision authority is user. No editor metadata '
            'or original photograph was changed.</p></section>'
        )
    audit = (
        "<section><h2>Audit and limitations</h2>"
        f"<p><strong>Coverage:</strong> {escape(_text(selection.coverage))}</p>"
        f"<p><strong>Constraints:</strong> {escape(_text(selection.unmet_constraints))}</p>"
        f"<p><strong>Profile:</strong> {escape(_text(session.profile_versions))}</p>"
        "<p><strong>Real-photo quality metrics: unavailable.</strong> This run does not prove "
        "burst-choice, nDCG, aesthetic quality, or editor compatibility.</p></section>"
    )
    template = files("silverloupe.resources").joinpath(
        "templates", "report", "index.html"
    ).read_text(encoding="utf-8")
    document = (
        template.replace("{{SUMMARY}}", summary)
        .replace("{{DISCLOSURE}}", disclosure)
        .replace("{{SECTIONS}}", sections)
        .replace("{{AUDIT}}", audit)
    )
    stylesheet = '@charset "UTF-8";\n' + "\n".join(
        preview_rules[name] for name in sorted(preview_rules)
    )
    return document, stylesheet


def _sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _report_marker(output: Path) -> dict[str, Any]:
    path = output / "report-generation.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _marker_records(marker: dict[str, Any], source: Path) -> dict[Path, set[str]]:
    publication_root = marker.get("publication_root")
    if not isinstance(publication_root, str):
        return {}
    if Path(publication_root).expanduser().resolve() != source:
        return {}
    owned: dict[Path, set[str]] = {}
    for key in ("files", "previous_files"):
        records = marker.get(key, [])
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            relative_value = record.get("path")
            digest = record.get("sha256")
            if not isinstance(relative_value, str) or not isinstance(digest, str):
                continue
            relative = Path(relative_value)
            if relative.is_absolute() or ".." in relative.parts:
                continue
            owned.setdefault(source / relative, set()).add(digest)
    return owned


def _validate_publication_target(path: Path, allowed_hashes: set[str]) -> None:
    if path.is_symlink():
        raise ValueError(f"report target is not owned by Silverloupe: {path}")
    if not path.exists():
        return
    if not path.is_file() or _sha256_path(path) not in allowed_hashes:
        raise ValueError(f"report target is not owned by Silverloupe: {path}")


def _remove_owned_legacy_report(output: Path, marker: dict[str, Any]) -> None:
    files_value = marker.get("files")
    generation_id = marker.get("generation_id")
    if files_value != ["report/index.html"] or not isinstance(generation_id, str):
        return
    legacy = output / "report/index.html"
    if legacy.is_symlink() or not legacy.is_file():
        return
    if _sha256_path(legacy) != generation_id:
        return
    legacy.unlink()
    try:
        legacy.parent.rmdir()
    except OSError:
        pass


def build_report(state: RunState) -> Path:
    """Publish one source-root report with non-importable hidden preview data."""

    output = Path(state.output_root).expanduser().resolve()
    validate_output_layout(output)
    document, stylesheet = _render(state)
    session = _read_json(output / "session.json", SessionConfig)
    if len(session.source_roots) != 1:
        raise ValueError("report publication requires exactly one source root")
    source = Path(session.source_roots[0]).expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"report source root does not exist: {source}")
    report_path = source / "report.html"
    report_assets = source / ".silverloupe-report"
    stylesheet_path = report_assets / "previews.css"
    previous_marker = _report_marker(output)
    owned = _marker_records(previous_marker, source)
    _validate_publication_target(report_path, owned.get(report_path, set()))
    _validate_publication_target(stylesheet_path, owned.get(stylesheet_path, set()))
    if report_assets.is_symlink() or (
        report_assets.exists()
        and (
            not report_assets.is_dir()
            or stylesheet_path not in owned
        )
    ):
        raise ValueError(
            f"report assets directory is not owned by Silverloupe: {report_assets}"
        )
    document_hash = hashlib.sha256(document.encode("utf-8")).hexdigest()
    stylesheet_hash = hashlib.sha256(stylesheet.encode("utf-8")).hexdigest()
    generation_id = hashlib.sha256(
        f"{document_hash}\0{stylesheet_hash}".encode("ascii")
    ).hexdigest()
    files_value = [
        {"path": "report.html", "sha256": document_hash},
        {
            "path": ".silverloupe-report/previews.css",
            "sha256": stylesheet_hash,
        },
    ]
    previous_files = [
        {
            "path": path.relative_to(source).as_posix(),
            "sha256": _sha256_path(path),
        }
        for path in (report_path, stylesheet_path)
        if path.is_file() and _sha256_path(path) in owned.get(path, set())
    ]
    marker = {
        "schema_version": 1,
        "generation_id": generation_id,
        "status": "publishing",
        "publication_root": str(source),
        "files": files_value,
        "previous_files": previous_files,
        "run_status": state.status,
        "session_id": session.session_id,
        "session_revision": state.revision,
    }
    marker_path = output / "report-generation.json"
    atomic_write_text(
        marker_path,
        json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    atomic_write_text(stylesheet_path, stylesheet)
    atomic_write_text(report_path, document)
    marker["status"] = "complete"
    atomic_write_text(
        marker_path,
        json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    _remove_owned_legacy_report(output, previous_marker)
    return report_path
