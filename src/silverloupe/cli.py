"""Command-line entry points for Silverloupe."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import sqlite3
import sys
import webbrowser
from pathlib import Path
from typing import Any, Sequence

from silverloupe import __version__
from silverloupe.contracts import (
    SCHEMA_VERSION,
    Asset,
    Brief,
    Decision,
    DeliveryPlan,
    DeliveryReceipt,
    InstallConfig,
    Manifest,
    RunConfig,
    RuntimeMode,
    SelectionItem,
    SelectionOverride,
    SessionConfig,
)
from silverloupe.curation import curate_prepared
from silverloupe.delivery import apply_delivery, plan_delivery, rollback_delivery
from silverloupe.grouping import group_prepared
from silverloupe.inventory import prepare, resume
from silverloupe.installer.capabilities import probe_install
from silverloupe.installer.config import configure_editor
from silverloupe.installer.lifecycle import (
    load_installed_config,
    read_release_manifest,
    uninstall_release,
    update_release,
)
from silverloupe.previews import validate_output_layout
from silverloupe.report import build_report
from silverloupe.state import atomic_write_text, read_status
from silverloupe.triage import (
    assess_prepared,
    create_vision_request_prepared,
    import_vision_response_prepared,
)
from silverloupe.workflow import apply_overrides, load_run_state, run_session


def doctor_report() -> dict[str, Any]:
    """Inspect optional capabilities without importing or modifying user libraries."""

    python_ready = sys.version_info[:2] == (3, 12)
    exiftool_path = shutil.which("exiftool")
    raw_available = importlib.util.find_spec("rawpy") is not None
    vision_value = os.environ.get("SILVERLOUPE_VISION_CAPABILITY", "unavailable").strip().lower()
    vision_status = vision_value if vision_value in {"ready", "degraded", "unavailable"} else "unavailable"

    report = {
        "python": {
            "status": "ready" if python_ready else "degraded",
            "version": platform.python_version(),
            "required": ">=3.12,<3.13",
        },
        "core": {"status": "ready", "version": __version__},
        "schema": {"status": "ready", "version": SCHEMA_VERSION},
        "exiftool": (
            {"status": "ready", "path": exiftool_path}
            if exiftool_path
            else {"status": "unavailable", "reason": "exiftool was not found on PATH"}
        ),
        "raw": (
            {"status": "ready", "backend": "rawpy"}
            if raw_available
            else {"status": "unavailable", "reason": "install the raw extra or use a verified editor renderer"}
        ),
        "vision": {
            "status": vision_status,
            "mode": "host-vision",
            **({"reason": "host vision capability was not declared"} if vision_status == "unavailable" else {}),
        },
    }
    installed = _installed_config(required=False)
    if installed is not None:
        report["installation"] = probe_install(installed)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="silverloupe")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="report runtime and optional capabilities")
    prepare_parser = subparsers.add_parser("prepare", help="inventory images and create previews")
    prepare_parser.add_argument("source")
    prepare_parser.add_argument("output")
    prepare_parser.add_argument("--max-edge", type=int, default=1600)
    status_parser = subparsers.add_parser("status", help="show persisted preparation status")
    status_parser.add_argument("output")
    resume_parser = subparsers.add_parser("resume", help="resume preparation from checkpoints")
    resume_parser.add_argument("output")
    group_parser = subparsers.add_parser("group", help="group a prepared session")
    group_parser.add_argument("output")
    group_mode = group_parser.add_mutually_exclusive_group()
    group_mode.add_argument(
        "--reuse-groups",
        action="store_true",
        help="validate edited groups.json and rebuild selection items without regrouping",
    )
    group_mode.add_argument(
        "--reset-manual-groups",
        action="store_true",
        help="discard saved manual groups and replace them with automatic grouping",
    )
    assess_parser = subparsers.add_parser(
        "assess", help="measure technical signals and propose review-only triage"
    )
    assess_parser.add_argument("output")
    vision_request_parser = subparsers.add_parser(
        "vision-request", help="write an auditable host-vision request"
    )
    vision_request_parser.add_argument("output")
    vision_import_parser = subparsers.add_parser(
        "vision-import", help="validate and import a host-vision response"
    )
    vision_import_parser.add_argument("output")
    vision_import_parser.add_argument("response")
    curate_parser = subparsers.add_parser(
        "curate", help="build genre-aware selects and an ordered top"
    )
    curate_parser.add_argument("output")
    run_parser = subparsers.add_parser("run", help="run the complete safe culling workflow")
    run_parser.add_argument("source")
    run_parser.add_argument("output")
    run_parser.add_argument("--brief", required=True, help="path to a strict Brief JSON document")
    report_parser = subparsers.add_parser("report", help="rebuild the static review report")
    report_parser.add_argument("output")
    review_parser = subparsers.add_parser("review", help="open an interactive local review")
    review_parser.add_argument("output")
    overrides_parser = subparsers.add_parser(
        "apply-overrides", help="apply revision-bound manual selection corrections"
    )
    overrides_parser.add_argument("output")
    overrides_parser.add_argument("overrides")
    delivery_plan_parser = subparsers.add_parser(
        "delivery-plan", help="build an explicit, non-mutating editor delivery plan"
    )
    delivery_plan_parser.add_argument("output")
    delivery_plan_parser.add_argument("--target", required=True)
    delivery_plan_parser.add_argument("--previous-receipt")
    delivery_plan_parser.add_argument("--accept-reject-hash")
    delivery_apply_parser = subparsers.add_parser(
        "delivery-apply", help="apply or resume the persisted delivery plan"
    )
    delivery_apply_parser.add_argument("output")
    delivery_rollback_parser = subparsers.add_parser(
        "delivery-rollback", help="roll back the persisted delivery receipt"
    )
    delivery_rollback_parser.add_argument("output")
    configure_parser = subparsers.add_parser(
        "configure", help="change the default editor for new sessions"
    )
    configure_parser.add_argument("--editor", required=True)
    configure_parser.add_argument(
        "--mode", required=True, choices=("native", "file", "report-only")
    )
    update_parser = subparsers.add_parser(
        "update", help="verify and activate another production release"
    )
    update_parser.add_argument("artifact")
    subparsers.add_parser("uninstall", help="remove only Silverloupe-owned files")
    return parser


def _brief(path: Path) -> Brief:
    try:
        return Brief.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"invalid brief JSON: {error}") from error


def _overrides(path: Path) -> list[SelectionOverride]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"invalid overrides JSON: {error}") from error
    if not isinstance(value, list):
        raise ValueError("invalid overrides JSON: expected a JSON array")
    try:
        return [SelectionOverride.model_validate(item) for item in value]
    except ValueError as error:
        raise ValueError(f"invalid overrides JSON: {error}") from error


def _session_revision(output: Path) -> int:
    return SessionConfig.model_validate_json(
        (output / "session.json").read_text(encoding="utf-8")
    ).revision


def _installed_config(*, required: bool) -> InstallConfig | None:
    raw_root = os.environ.get("SILVERLOUPE_INSTALL_ROOT")
    if not raw_root:
        if required:
            raise ValueError("SILVERLOUPE_INSTALL_ROOT is not set")
        return None
    try:
        return load_installed_config(Path(raw_root).expanduser().resolve())
    except FileNotFoundError:
        if required:
            raise
        return None


def _installed_run_settings() -> dict[str, Any]:
    config = _installed_config(required=False)
    if config is None:
        return {}
    runtime_mode = {
        "host-vision": RuntimeMode.HOST_VISION,
        "api": RuntimeMode.API,
        "local-signals": RuntimeMode.LOCAL,
        "local-vision": RuntimeMode.LOCAL,
    }[config.model_mode]
    return {
        "runtime_mode": runtime_mode,
        "budget": {
            "cache_quota": config.cache_quota,
            "worker_limit": config.worker_limit,
        },
        "editor_config_snapshot": {
            "config_version": config.config_version,
            "enabled_editors": list(config.enabled_editors),
            "default_editor": config.default_editor,
            "integration_mode": config.integration_mode_by_editor[
                config.default_editor
            ],
            "metadata_mapping": config.metadata_mapping_by_editor.get(
                config.default_editor, {}
            ),
            "runtime_version": config.runtime_version,
        },
    }


def _model_file(path: Path, model: type[Any]) -> Any:
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise FileNotFoundError(f"required artifact does not exist: {path}") from error
    except (OSError, ValueError) as error:
        raise ValueError(f"invalid {path.name}: {error}") from error


def _jsonl_models(path: Path, model: type[Any]) -> list[Any]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as error:
        raise FileNotFoundError(f"required artifact does not exist: {path}") from error
    result: list[Any] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            result.append(model.model_validate_json(line))
        except ValueError as error:
            raise ValueError(f"invalid {path.name} line {line_number}: {error}") from error
    return result


def _json_array_models(path: Path, model: type[Any]) -> list[Any]:
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
    except ValueError as error:
        raise ValueError(f"invalid {path.name}: {error}") from error


def _require_delivery_session(output: Path) -> tuple[SessionConfig, Manifest, list[SelectionItem], list[Decision]]:
    validate_output_layout(output)
    state = load_run_state(output)
    if state.status != "completed":
        raise ValueError("delivery planning requires a completed curation run")
    try:
        generation = json.loads((output / "curation-generation.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"invalid curation-generation.json: {error}") from error
    if not isinstance(generation, dict) or generation.get("status") != "complete":
        raise ValueError("delivery planning requires a completed curation generation")
    session = _model_file(output / "session.json", SessionConfig)
    if len(session.source_roots) != 1:
        raise ValueError("delivery planning currently requires exactly one source root")
    manifest = Manifest(assets=_jsonl_models(output / "assets.jsonl", Asset))
    items = _json_array_models(output / "selection-items.json", SelectionItem)
    decisions = _jsonl_models(output / "decisions.jsonl", Decision)
    return session, manifest, items, decisions


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "doctor":
            print(json.dumps(doctor_report(), ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "configure":
            installed = _installed_config(required=True)
            assert installed is not None
            configured = configure_editor(installed, args.editor, args.mode)
            release = Path(installed.install_root) / "versions" / installed.runtime_version
            result = update_release(release, configured)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "update":
            installed = _installed_config(required=True)
            assert installed is not None
            artifact = Path(args.artifact).expanduser().resolve()
            manifest = read_release_manifest(artifact)
            configured = InstallConfig.model_validate(
                {
                    **installed.model_dump(mode="json"),
                    "runtime_version": manifest["release_version"],
                }
            )
            result = update_release(artifact, configured)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "uninstall":
            installed = _installed_config(required=True)
            assert installed is not None
            result = uninstall_release(installed)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "prepare":
            source = Path(args.source).expanduser().resolve()
            output = Path(args.output).expanduser().resolve()
            config = RunConfig(
                source_roots=[str(source)],
                output_root=str(output),
                brief=Brief(),
                rendering_policy={"max_edge": args.max_edge},
                **_installed_run_settings(),
            )
            manifest = prepare(source, output, config)
            unavailable = sum(asset.decode_status != "ready" for asset in manifest.assets)
            print(
                json.dumps(
                    {
                        "status": "completed",
                        "assets": len(manifest.assets),
                        "unavailable": unavailable,
                        "output": str(output),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "status":
            print(json.dumps(read_status(Path(args.output)), ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "resume":
            output = Path(args.output).expanduser().resolve()
            manifest = resume(output)
            print(
                json.dumps(
                    {
                        "status": "completed",
                        "assets": len(manifest.assets),
                        "unavailable": sum(asset.decode_status != "ready" for asset in manifest.assets),
                        "output": str(output),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "group":
            output = Path(args.output).expanduser().resolve()
            report = group_prepared(
                output,
                reuse_groups=args.reuse_groups,
                reset_manual_groups=args.reset_manual_groups,
            )
            print(
                json.dumps(
                    {
                        "status": "completed",
                        "assets": report["assets"],
                        "groups": report["groups"],
                        "selection_items": report["selection_items"],
                        "review_groups": len(report["review_group_ids"]),
                        "output": str(output),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "assess":
            output = Path(args.output).expanduser().resolve()
            report = assess_prepared(output)
            print(
                json.dumps(
                    {
                        "status": report["status"],
                        "assets": report["assets"],
                        "vision": report["vision"],
                        "auto_reject_enabled": report["auto_reject_enabled"],
                        "output": str(output),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0 if report["status"] == "completed" else 3
        if args.command == "vision-request":
            output = Path(args.output).expanduser().resolve()
            request = create_vision_request_prepared(output)
            print(
                json.dumps(
                    {
                        "status": "pending",
                        "request_id": request.request_id,
                        "attempt": request.attempt,
                        "assets": len(request.assets),
                        "request": str(output / "vision-request.json"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "vision-import":
            output = Path(args.output).expanduser().resolve()
            response = Path(args.response).expanduser().resolve()
            result = import_vision_response_prepared(output, response)
            print(
                json.dumps(
                    {
                        "status": result.status,
                        "assets": len(result.assessments),
                        "next_attempt": (
                            result.next_request.attempt if result.next_request is not None else None
                        ),
                        "output": str(output),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0 if result.status == "completed" else 3
        if args.command == "curate":
            output = Path(args.output).expanduser().resolve()
            report = curate_prepared(output)
            print(
                json.dumps(
                    {
                        "status": report["status"],
                        "items": report["items"],
                        "selects": report["selects"],
                        "top": report["top"],
                        "unmet_constraints": report["unmet_constraints"],
                        "output": str(output),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "run":
            source = Path(args.source).expanduser().resolve()
            output = Path(args.output).expanduser().resolve()
            state = run_session(
                RunConfig(
                    source_roots=[str(source)],
                    output_root=str(output),
                    brief=_brief(Path(args.brief).expanduser().resolve()),
                    **_installed_run_settings(),
                )
            )
            report_path = build_report(state)
            print(
                json.dumps(
                    {
                        "status": state.status,
                        "session_id": state.session_id,
                        "revision": _session_revision(output),
                        "output": str(output),
                        "report": str(report_path),
                        "errors": state.errors,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0 if state.status == "completed" else 3
        if args.command == "report":
            output = Path(args.output).expanduser().resolve()
            state = load_run_state(output)
            report_path = build_report(state)
            print(
                json.dumps(
                    {
                        "status": state.status,
                        "revision": _session_revision(output),
                        "report": str(report_path),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0 if state.status == "completed" else 3
        if args.command == "review":
            from silverloupe.review_server import create_review_server

            server = create_review_server(Path(args.output))
            print(f"Interactive review: {server.url}", flush=True)
            print("Keep this terminal open while reviewing. Press Ctrl+C to stop.", flush=True)
            webbrowser.open(server.url)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return 0
        if args.command == "apply-overrides":
            output = Path(args.output).expanduser().resolve()
            state = apply_overrides(
                output, _overrides(Path(args.overrides).expanduser().resolve())
            )
            report_path = build_report(state)
            print(
                json.dumps(
                    {
                        "status": state.status,
                        "revision": _session_revision(output),
                        "report": str(report_path),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "delivery-plan":
            output = Path(args.output).expanduser().resolve()
            session, manifest, items, decisions = _require_delivery_session(output)
            previous = (
                _model_file(Path(args.previous_receipt).expanduser().resolve(), DeliveryReceipt)
                if args.previous_receipt
                else None
            )
            plan = plan_delivery(
                manifest,
                items,
                decisions,
                args.target,
                source_root=Path(session.source_roots[0]),
                output_root=output,
                previous_receipt=previous,
                accepted_reject_hash=args.accept_reject_hash,
            )
            plan_path = output / "delivery-plan.json"
            atomic_write_text(plan_path, plan.model_dump_json(indent=2) + "\n")
            print(
                json.dumps(
                    {
                        "status": plan.status,
                        "backend": plan.backend,
                        "changes": len(plan.proposed_changes),
                        "conflicts": len(plan.conflicts),
                        "plan": str(plan_path),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "delivery-apply":
            output = Path(args.output).expanduser().resolve()
            validate_output_layout(output)
            plan = _model_file(output / "delivery-plan.json", DeliveryPlan)
            if Path(plan.output_root or "").expanduser().resolve() != output:
                raise ValueError("delivery plan output_root does not match requested output")
            receipt = apply_delivery(plan)
            receipt_path = output / "delivery-receipt.json"
            atomic_write_text(receipt_path, receipt.model_dump_json(indent=2) + "\n")
            counts = {
                status: sum(outcome.status == status for outcome in receipt.outcomes)
                for status in ("applied", "skipped", "failed")
            }
            print(
                json.dumps(
                    {
                        "status": receipt.status,
                        **counts,
                        "receipt": str(receipt_path),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0 if receipt.status == "complete" else 3
        if args.command == "delivery-rollback":
            output = Path(args.output).expanduser().resolve()
            validate_output_layout(output)
            receipt = _model_file(output / "delivery-receipt.json", DeliveryReceipt)
            if Path(receipt.output_root or "").expanduser().resolve() != output:
                raise ValueError("delivery receipt output_root does not match requested output")
            rolled_back = rollback_delivery(receipt)
            receipt_path = output / "delivery-rollback-receipt.json"
            atomic_write_text(receipt_path, rolled_back.model_dump_json(indent=2) + "\n")
            print(
                json.dumps(
                    {
                        "status": rolled_back.status,
                        "applied": sum(
                            outcome.status == "applied" for outcome in rolled_back.outcomes
                        ),
                        "conflicts": sum(
                            outcome.conflict is not None for outcome in rolled_back.outcomes
                        ),
                        "receipt": str(receipt_path),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0 if rolled_back.status == "rolled_back" else 3
    except (FileNotFoundError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 4
    return 2
