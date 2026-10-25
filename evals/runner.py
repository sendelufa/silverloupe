"""Deterministic artifact validation and grading for development evals."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any, Iterable, Sequence, TypeVar

from pydantic import BaseModel, ValidationError

from silverloupe.contracts import (
    Assessment,
    Decision,
    DeliveryPlan,
    Manifest,
    Selection,
    SelectionItem,
)

from evals.contracts import TrialResult
from evals.host_adapters import SUPPORTED_HOSTS, execute_host
from evals.metrics import summarize_trials
from evals.release import qualify_release


ModelT = TypeVar("ModelT", bound=BaseModel)
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SOURCE_ROOT = Path(__file__).parents[1]


def _trial_prompt(case: dict[str, Any], variant: str) -> str:
    prompt = case.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("trial case requires a non-empty prompt")
    return (
        f"{prompt}\n\n"
        "This is a development evaluation. Return only one JSON object with these keys: "
        'status (passed, partial, or failed), selected_skill (string or null), disclosures '
        "(array of strings), reference_reads (array of paths), artifact_paths (array of paths), "
        f"and errors (array of strings). Evaluation variant: {variant}."
    )


def _trial_errors(case: dict[str, Any], observation: Any) -> list[str]:
    expected = case.get("expected", {})
    if not isinstance(expected, dict):
        return ["case expected contract must be an object"]
    errors: list[str] = []
    if "selected_skill" in expected and observation.selected_skill != expected["selected_skill"]:
        errors.append(
            f"selected skill mismatch: expected {expected['selected_skill']!r}, "
            f"got {observation.selected_skill!r}"
        )
    required = expected.get("required_disclosures", [])
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        errors.append("required_disclosures must be an array of strings")
    else:
        missing = sorted(set(required) - set(observation.disclosures))
        if missing:
            errors.append(f"missing required disclosures: {', '.join(missing)}")
    return errors


def _prepare_artifact(artifact: Path, host: str, workspace: Path) -> dict[str, Any]:
    artifact = artifact.expanduser().resolve(strict=True)
    extracted_root = workspace / ".silverloupe-release"
    extracted_root.mkdir()
    roots: set[str] = set()
    with tarfile.open(artifact, "r:gz") as archive:
        for member in archive.getmembers():
            member_path = Path(member.name)
            if (
                member_path.is_absolute()
                or ".." in member_path.parts
                or member.issym()
                or member.islnk()
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError(f"unsafe release archive member: {member.name}")
            if member_path.parts:
                roots.add(member_path.parts[0])
        if len(roots) != 1:
            raise ValueError("release archive must contain exactly one root")
        archive.extractall(extracted_root, filter="data")
    release_root = extracted_root / next(iter(roots))
    installer = release_root / "install.py"
    if not installer.is_file():
        raise ValueError("release archive does not contain install.py at its root")
    command = [
        sys.executable,
        str(installer),
        "--non-interactive",
        "--host",
        "codex" if host == "codex" else "claude-code",
        "--editor",
        "report-only",
        "--mode",
        "report-only",
        "--scope",
        "project",
        "--install-root",
        str(workspace),
    ]
    completed = subprocess.run(
        command,
        cwd=workspace,
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic"
        raise RuntimeError(
            f"release installer failed with exit code {completed.returncode}: {detail}"
        )
    return {
        "path": str(artifact),
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "installer_command": command,
    }


def run_trial(
    case: dict[str, Any], host: str, variant: str, output_dir: Path
) -> TrialResult:
    """Run one host trial in a new directory and persist immutable evidence."""

    if host not in SUPPORTED_HOSTS:
        raise ValueError(f"unsupported eval host: {host}")
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError("trial case requires a non-empty case_id")
    prompt = _trial_prompt(case, variant)
    output_dir = output_dir.expanduser()
    if output_dir.exists():
        raise FileExistsError(f"trial output already exists: {output_dir}")
    output_dir.mkdir(parents=True)
    workspace = output_dir / "workspace"
    workspace.mkdir()
    (output_dir / "case.json").write_text(
        json.dumps(case, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    artifact_metadata: dict[str, Any] | None = None
    preparation_errors: list[str] = []
    artifact_value = case.get("artifact")
    if artifact_value is not None:
        if not isinstance(artifact_value, str) or not artifact_value.strip():
            preparation_errors.append("case artifact must be a non-empty path string")
        else:
            try:
                artifact_metadata = _prepare_artifact(Path(artifact_value), host, workspace)
            except (OSError, ValueError, RuntimeError, tarfile.TarError) as error:
                preparation_errors.append(f"could not install release artifact: {error}")
    execution = (
        execute_host(host, prompt, workspace)
        if not preparation_errors
        else None
    )
    transcript = output_dir / "transcript.jsonl"
    trace = output_dir / "tool-trace.log"
    transcript.write_text(execution.stdout if execution else "", encoding="utf-8")
    trace.write_text(execution.stderr if execution else "", encoding="utf-8")
    errors = [*preparation_errors, *(execution.errors if execution else ())]
    observation = execution.observation if execution else None
    if observation is not None:
        errors.extend(_trial_errors(case, observation))
        errors.extend(observation.errors)
    status = "failed" if errors or observation is None else observation.status
    result = TrialResult(
        case_id=case_id,
        host=host,
        variant=variant,
        output_dir=str(output_dir.resolve()),
        status=status,
        transcript_path=str(transcript.resolve()),
        tool_trace_path=str(trace.resolve()),
        wall_time_seconds=execution.wall_time_seconds if execution else 0.0,
        tokens=execution.tokens if execution else {},
        cost=execution.cost if execution else {},
        versions=(
            {"host_cli": execution.version}
            if execution is not None and execution.version is not None
            else {}
        ),
        errors=errors,
        metadata={
            "command": list(execution.command) if execution else [],
            "exit_code": execution.exit_code if execution else None,
            **({"artifact": artifact_metadata} if artifact_metadata is not None else {}),
            **(
                {"observation": observation.model_dump(mode="json")}
                if observation is not None
                else {}
            ),
        },
    )
    (output_dir / "trial-result.json").write_text(
        result.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return result


def _load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {error.msg}") from error
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: case must be an object")
            case_id = value.get("case_id")
            if not isinstance(case_id, str) or not _SAFE_COMPONENT.fullmatch(case_id):
                raise ValueError(f"{path}:{line_number}: unsafe or missing case_id")
            if "expected" not in value:
                value["expected"] = {
                    "selected_skill": value.get("expected_skill"),
                    "required_disclosures": value.get("required_disclosures", []),
                }
            cases.append(value)
    if not cases:
        raise ValueError(f"no eval cases found in {path}")
    return cases


def _csv(value: str, *, label: str) -> list[str]:
    values = [item.strip() for item in value.split(",") if item.strip()]
    if not values:
        raise ValueError(f"{label} must contain at least one value")
    if len(values) != len(set(values)):
        raise ValueError(f"{label} contains duplicate values")
    if not all(_SAFE_COMPONENT.fullmatch(item) for item in values):
        raise ValueError(f"{label} contains an unsafe value")
    return values


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals.runner")
    parser.add_argument("--suite", choices=("routing", "release"), required=True)
    parser.add_argument("--hosts", required=True, help="comma-separated codex,claude")
    parser.add_argument("--variants", default="b3", help="comma-separated eval variants")
    parser.add_argument("--trials", type=int, required=True)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _cli_parser().parse_args(argv)
    try:
        hosts = _csv(args.hosts, label="hosts")
        variants = _csv(args.variants, label="variants")
        unknown_hosts = sorted(set(hosts) - set(SUPPORTED_HOSTS))
        if unknown_hosts:
            raise ValueError(f"unsupported hosts: {', '.join(unknown_hosts)}")
        if args.trials < 1:
            raise ValueError("--trials must be at least 1")
        if args.suite == "release" and args.artifact is None:
            raise ValueError("--artifact is required for the release suite")
        output = args.output.expanduser()
        if output.exists():
            raise ValueError(f"output already exists: {output}")
        cases_path = (
            args.cases.expanduser()
            if args.cases is not None
            else _SOURCE_ROOT / "evals/cases/routing.jsonl"
        )
        cases = _load_cases(cases_path)
        preflight: dict[str, Any] | None = None
        artifact: Path | None = None
        if args.artifact is not None:
            artifact = args.artifact.expanduser().resolve(strict=True)
            preflight = qualify_release(artifact, source_root=_SOURCE_ROOT)
            if preflight["decision"] == "blocked":
                raise ValueError("release artifact failed qualification preflight")
    except (FileNotFoundError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2

    output.mkdir(parents=True)
    results: list[TrialResult] = []
    result_paths: list[str] = []
    for case in cases:
        prepared_case = dict(case)
        if artifact is not None:
            prepared_case["artifact"] = str(artifact)
        for host in hosts:
            for variant in variants:
                for trial_number in range(1, args.trials + 1):
                    trial_dir = (
                        output
                        / "trials"
                        / prepared_case["case_id"]
                        / host
                        / variant
                        / str(trial_number)
                    )
                    result = run_trial(prepared_case, host, variant, trial_dir)
                    results.append(result)
                    result_paths.append(
                        (trial_dir / "trial-result.json").relative_to(output).as_posix()
                    )
    summary = {
        "schema_version": 1,
        "suite": args.suite,
        "suite_scope": "routing-disclosure" if args.suite == "release" else "routing",
        "cases": len(cases),
        "hosts": hosts,
        "variants": variants,
        "trials_per_combination": args.trials,
        "artifact": preflight["artifact"] if preflight is not None else None,
        "trial_summary": summarize_trials(results),
        "results": result_paths,
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if artifact is not None:
        qualification = qualify_release(
            artifact, source_root=_SOURCE_ROOT, evidence_root=output
        )
        (output / "qualification.json").write_text(
            json.dumps(qualification, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    return 0 if all(result.status == "passed" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _load_jsonl(path: Path) -> list[Any]:
    values: list[Any] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line.strip():
                try:
                    values.append(json.loads(line))
                except json.JSONDecodeError as error:
                    raise ValueError(f"{path.name}:{line_number}: invalid JSON: {error.msg}") from error
    return values


def _models(model: type[ModelT], values: Iterable[Any], source: str, errors: list[str]) -> list[ModelT]:
    parsed: list[ModelT] = []
    for index, value in enumerate(values):
        try:
            parsed.append(model.model_validate(value))
        except ValidationError as error:
            errors.append(f"{source}[{index}]: {error}")
    return parsed


def _load_required(path: Path, loader: Any, errors: list[str]) -> Any | None:
    if not path.is_file():
        errors.append(f"missing required artifact: {path.name}")
        return None
    try:
        return loader(path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        errors.append(f"{path.name}: {error}")
        return None


def _json_array(value: Any, source: str, errors: list[str]) -> list[Any]:
    if not isinstance(value, list):
        errors.append(f"{source} must contain a JSON array")
        return []
    return value


def _parsed_run(path: Path) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if not path.is_dir():
        return {}, [f"run path is not a directory: {path}"]

    assets_path = path / "assets.jsonl"
    legacy_manifest_path = path / "manifest.json"
    if assets_path.is_file():
        assets_value = _load_required(assets_path, _load_jsonl, errors)
        manifest_value = {"schema_version": 1, "assets": assets_value or []}
    elif legacy_manifest_path.is_file():
        manifest_value = _load_required(legacy_manifest_path, _load_json, errors)
    else:
        errors.append("missing required artifact: assets.jsonl")
        manifest_value = None
    items_value = _load_required(path / "selection-items.json", _load_json, errors)
    assessments_value = _load_required(path / "assessments.jsonl", _load_jsonl, errors)
    decisions_value = _load_required(path / "decisions.jsonl", _load_jsonl, errors)
    selection_value = _load_required(path / "selection.json", _load_json, errors)

    manifest: Manifest | None = None
    selection: Selection | None = None
    if manifest_value is not None:
        try:
            manifest = Manifest.model_validate(manifest_value)
        except ValidationError as error:
            errors.append(f"manifest.json: {error}")
    if selection_value is not None:
        try:
            selection = Selection.model_validate(selection_value)
        except ValidationError as error:
            errors.append(f"selection.json: {error}")

    items = _models(
        SelectionItem,
        _json_array(items_value, "selection-items.json", errors),
        "selection-items.json",
        errors,
    )
    assessments = _models(
        Assessment,
        _json_array(assessments_value, "assessments.jsonl", errors),
        "assessments.jsonl",
        errors,
    )
    decisions = _models(
        Decision,
        _json_array(decisions_value, "decisions.jsonl", errors),
        "decisions.jsonl",
        errors,
    )

    delivery_plan: DeliveryPlan | None = None
    delivery_path = path / "delivery-plan.json"
    if delivery_path.is_file():
        try:
            delivery_plan = DeliveryPlan.model_validate(_load_json(delivery_path))
        except (ValidationError, OSError, ValueError, json.JSONDecodeError) as error:
            errors.append(f"delivery-plan.json: {error}")

    asset_ids = {asset.asset_id for asset in manifest.assets} if manifest else set()
    item_ids = {item.item_id for item in items}
    items_by_id = {item.item_id: item for item in items}
    if len(item_ids) != len(items):
        errors.append("selection-items.json: duplicate item_id values")

    for item in items:
        for asset_id in item.member_asset_ids:
            if asset_id not in asset_ids:
                errors.append(f"selection item '{item.item_id}' references unknown asset_id '{asset_id}'")
    for assessment in assessments:
        if assessment.asset_id not in asset_ids:
            errors.append(f"assessment references unknown asset_id '{assessment.asset_id}'")
    for decision in decisions:
        if decision.item_id not in item_ids:
            errors.append(f"decision references unknown item_id '{decision.item_id}'")
    if selection:
        for field_name in ("selects_ids", "top_ids", "ranked_ids"):
            for item_id in getattr(selection, field_name):
                if item_id not in item_ids:
                    errors.append(f"selection.{field_name} references unknown item_id '{item_id}'")
    if delivery_plan:
        for mapping in delivery_plan.asset_mappings:
            if mapping.asset_id not in asset_ids:
                errors.append(f"delivery mapping references unknown asset_id '{mapping.asset_id}'")
        for change in delivery_plan.proposed_changes:
            if change.asset_id not in asset_ids:
                errors.append(f"delivery change references unknown asset_id '{change.asset_id}'")
            if change.item_id is not None and change.item_id not in item_ids:
                errors.append(f"delivery change references unknown item_id '{change.item_id}'")
            elif (
                change.item_id is not None
                and change.asset_id not in items_by_id[change.item_id].member_asset_ids
            ):
                errors.append(
                    f"delivery change asset_id '{change.asset_id}' is not a member of item_id "
                    f"'{change.item_id}'"
                )

    return {
        "manifest": manifest,
        "items": items,
        "assessments": assessments,
        "decisions": decisions,
        "selection": selection,
        "delivery_plan": delivery_plan,
    }, errors


def validate_run(path: Path) -> list[str]:
    """Return deterministic validation errors for a run artifact directory."""

    _, errors = _parsed_run(path)
    return errors


def _check(passed: bool, message: str, **details: Any) -> dict[str, Any]:
    return {"passed": passed, "message": message, **details}


def grade_artifacts(case: dict[str, Any], output: Path) -> dict[str, Any]:
    """Grade contract, selection-size, and rejection-safety invariants."""

    parsed, errors = _parsed_run(output)
    items: list[SelectionItem] = parsed.get("items", [])
    manifest: Manifest | None = parsed.get("manifest")
    assessments: list[Assessment] = parsed.get("assessments", [])
    decisions: list[Decision] = parsed.get("decisions", [])
    selection: Selection | None = parsed.get("selection")
    delivery_plan: DeliveryPlan | None = parsed.get("delivery_plan")

    top_ids = selection.top_ids if selection else []
    expected_k = case.get("brief", {}).get("top_k")
    allowed_shortfall = case.get("allowed_top_shortfall")
    expected_shortfall_constraints = (
        allowed_shortfall.get("unmet_constraints", [])
        if isinstance(allowed_shortfall, dict)
        else []
    )
    actual_unmet_constraints = selection.unmet_constraints if selection else []
    honest_shortfall = (
        isinstance(expected_k, int)
        and len(top_ids) < expected_k
        and bool(expected_shortfall_constraints)
        and set(actual_unmet_constraints) == set(expected_shortfall_constraints)
    )
    requested_k_passed = expected_k is None or len(top_ids) == expected_k or honest_shortfall

    rejected = {decision.item_id for decision in decisions if decision.status.value == "reject"}
    all_item_ids = {item.item_id for item in items}
    reject_all = bool(all_item_ids) and all_item_ids.issubset(rejected)

    proposed_rejects = {
        decision.item_id
        for decision in decisions
        if decision.status.value == "reject" and decision.decision_authority.value == "proposal"
    }
    items_by_id = {item.item_id: item for item in items}
    proposed_reject_assets = {
        asset_id
        for item_id in proposed_rejects
        if item_id in items_by_id
        for asset_id in items_by_id[item_id].member_asset_ids
    }
    exported_proposed_rejects: list[str] = []
    if delivery_plan:
        for change in delivery_plan.proposed_changes:
            is_reject_field = change.field in {"reject", "reject_flag", "pick_status"}
            is_reject_value = change.value is True or str(change.value).lower() in {"reject", "rejected"}
            belongs_to_proposed_reject = (
                change.item_id in proposed_rejects
                or change.asset_id in proposed_reject_assets
            )
            if belongs_to_proposed_reject and is_reject_field and is_reject_value:
                exported_proposed_rejects.append(change.item_id or change.asset_id)

    gold_good = set(case.get("gold_good_item_ids", []))
    rejected_gold = sorted(gold_good & rejected)
    manifest_asset_ids = {asset.asset_id for asset in manifest.assets} if manifest else set()
    represented_asset_ids = {asset_id for item in items for asset_id in item.member_asset_ids}
    assessed_asset_ids = [assessment.asset_id for assessment in assessments]
    decided_item_ids = [decision.item_id for decision in decisions]
    missing_assessments = sorted(manifest_asset_ids - set(assessed_asset_ids))
    unrepresented_assets = sorted(manifest_asset_ids - represented_asset_ids)
    missing_decisions = sorted(all_item_ids - set(decided_item_ids))
    missing_gold = sorted(gold_good - (all_item_ids & set(decided_item_ids)))
    complete_accounting = (
        not missing_assessments
        and not unrepresented_assets
        and not missing_decisions
        and len(assessed_asset_ids) == len(set(assessed_asset_ids))
        and len(decided_item_ids) == len(set(decided_item_ids))
    )

    checks = {
        "valid_references": _check(not errors, "all artifact references and schemas are valid"),
        "requested_k": _check(
            requested_k_passed,
            "top size is counted in SelectionItem IDs",
            actual=len(top_ids),
            expected=expected_k,
            unmet_constraints=actual_unmet_constraints,
        ),
        "complete_accounting": _check(
            complete_accounting,
            "every asset has one assessment and every selection item has one decision",
            missing_asset_ids=missing_assessments,
            unrepresented_asset_ids=unrepresented_assets,
            missing_item_ids=missing_decisions,
        ),
        "no_reject_all": _check(
            bool(case.get("allow_reject_all")) or not reject_all,
            "the output must not reject every selection item unless the case explicitly permits it",
            rejected=len(rejected),
            total=len(all_item_ids),
        ),
        "proposed_reject_not_exported": _check(
            not exported_proposed_rejects,
            "proposal-authority rejects must not be exported as reject flags",
            item_ids=sorted(set(exported_proposed_rejects)),
        ),
        "gold_good_retained": _check(
            not rejected_gold and not missing_gold,
            "gold-good items must be present, decided, and not rejected",
            rejected_item_ids=rejected_gold,
            missing_item_ids=missing_gold,
        ),
    }
    failed_messages = [check["message"] for check in checks.values() if not check["passed"]]
    return {
        "case_id": case.get("case_id"),
        "passed": all(check["passed"] for check in checks.values()),
        "checks": checks,
        "errors": [*errors, *failed_messages],
    }
