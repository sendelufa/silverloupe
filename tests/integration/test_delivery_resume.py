from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import silverloupe.delivery.base as delivery_base
from silverloupe.contracts import (
    Asset,
    Brief,
    Decision,
    DecisionStatus,
    Manifest,
    ProposedChange,
    RunState,
    SelectionItem,
    SelectionTier,
    SessionConfig,
)
from silverloupe.cli import main
from silverloupe.delivery import apply_delivery, plan_delivery, rollback_delivery
from silverloupe.delivery.xmp import merge_xmp, read_xmp


def _write_raw(root: Path, name: str, content: bytes) -> Asset:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return Asset(
        asset_id=hashlib.sha256(name.encode("utf-8")).hexdigest()[:12],
        relative_path=name,
        content_sha256=hashlib.sha256(content).hexdigest(),
        media_type="image/x-raw",
    )


def _item(asset: Asset) -> SelectionItem:
    return SelectionItem(
        item_id=f"item-{asset.asset_id}",
        kind="capture",
        member_asset_ids=[asset.asset_id],
        primary_asset_id=asset.asset_id,
    )


def _decision(asset: Asset, tier: SelectionTier) -> Decision:
    return Decision(
        item_id=f"item-{asset.asset_id}",
        status=DecisionStatus.KEEP,
        tier=tier,
    )


def _plan(
    source: Path,
    output: Path,
    assets: list[Asset],
    tiers: list[SelectionTier],
    *,
    previous=None,
):
    return plan_delivery(
        Manifest(assets=assets),
        [_item(asset) for asset in assets],
        [_decision(asset, tier) for asset, tier in zip(assets, tiers, strict=True)],
        "xmp-sidecar",
        source_root=source,
        output_root=output,
        previous_receipt=previous,
    )


def _sidecar(source: Path, asset: Asset) -> Path:
    return (source / asset.relative_path).with_suffix(".xmp")


def _xmp_with_keywords(*keywords: str) -> bytes:
    return merge_xmp(
        None,
        [
            ProposedChange(
                asset_id="fixture",
                item_id="fixture",
                field="keywords",
                value=list(keywords),
            )
        ],
        {},
    )[0]


def _write_completed_session(output: Path, source: Path, assets: list[Asset]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    items = [_item(asset) for asset in assets]
    decisions = [_decision(asset, SelectionTier.TOP) for asset in assets]
    session = SessionConfig(
        session_id="delivery-session",
        source_roots=[str(source)],
        brief=Brief(),
        revision=1,
    )
    state = RunState(
        session_id=session.session_id,
        revision=1,
        status="completed",
        output_root=str(output.resolve()),
        artifact_paths={},
    )
    (output / "session.json").write_text(session.model_dump_json(indent=2) + "\n")
    (output / "assets.jsonl").write_text(
        "".join(asset.model_dump_json() + "\n" for asset in assets)
    )
    (output / "selection-items.json").write_text(
        json.dumps([item.model_dump(mode="json") for item in items]) + "\n"
    )
    (output / "decisions.jsonl").write_text(
        "".join(decision.model_dump_json() + "\n" for decision in decisions)
    )
    (output / "curation-generation.json").write_text(
        json.dumps({"schema_version": 1, "status": "complete"}) + "\n"
    )
    (output / "run-state.json").write_text(state.model_dump_json(indent=2) + "\n")


def test_apply_skips_changed_source_and_changed_sidecar_preimages(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    changed_source = _write_raw(source, "source.cr2", b"source-before")
    changed_sidecar = _write_raw(source, "sidecar.nef", b"sidecar-source")
    sidecar_path = _sidecar(source, changed_sidecar)
    sidecar_path.write_bytes(_xmp_with_keywords("before"))
    plan = _plan(
        source,
        output,
        [changed_source, changed_sidecar],
        [SelectionTier.TOP, SelectionTier.SELECT],
    )
    (source / changed_source.relative_path).write_bytes(b"source-after")
    sidecar_path.write_bytes(_xmp_with_keywords("after"))

    receipt = apply_delivery(plan)

    assert receipt.status == "partial"
    assert [outcome.status for outcome in receipt.outcomes] == ["failed", "failed"]
    assert "source preimage changed" in (receipt.outcomes[0].error or "")
    assert "metadata preimage changed" in (receipt.outcomes[1].error or "")
    assert not _sidecar(source, changed_source).exists()
    assert read_xmp(sidecar_path)["keywords"] == ["after"]


def test_partial_apply_resumes_and_repeated_apply_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    first = _write_raw(source, "first.cr2", b"first")
    second = _write_raw(source, "second.nef", b"second")
    plan = _plan(
        source,
        output,
        [first, second],
        [SelectionTier.TOP, SelectionTier.SELECT],
    )
    real_commit = delivery_base.commit_staged_xmp
    injected = False

    def fail_second_once(staged):
        nonlocal injected
        if staged.asset_id == second.asset_id and not injected:
            injected = True
            raise OSError("simulated second-sidecar interruption")
        return real_commit(staged)

    monkeypatch.setattr(delivery_base, "commit_staged_xmp", fail_second_once)

    partial = apply_delivery(plan)
    resumed = apply_delivery(plan)
    hashes_after_resume = {
        asset.asset_id: hashlib.sha256(_sidecar(source, asset).read_bytes()).hexdigest()
        for asset in (first, second)
    }
    repeated = apply_delivery(plan)

    assert partial.status == "partial"
    assert [outcome.status for outcome in partial.outcomes] == ["applied", "failed"]
    assert resumed.status == "complete"
    assert [outcome.status for outcome in resumed.outcomes] == ["skipped", "applied"]
    assert repeated.status == "complete"
    assert [outcome.status for outcome in repeated.outcomes] == ["skipped", "skipped"]
    assert hashes_after_resume == {
        asset.asset_id: hashlib.sha256(_sidecar(source, asset).read_bytes()).hexdigest()
        for asset in (first, second)
    }
    assert read_xmp(_sidecar(source, first))["keywords"] == ["silverloupe:top"]
    assert read_xmp(_sidecar(source, second))["keywords"] == ["silverloupe:select"]


def test_resume_recovers_postimage_when_applied_journal_append_was_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    asset = _write_raw(source, "journal-gap.cr2", b"journal-gap")
    plan = _plan(source, output, [asset], [SelectionTier.TOP])
    real_append = delivery_base._append_journal
    interrupted = False

    def interrupt_applied_record_once(path, record):
        nonlocal interrupted
        if record.get("status") == "applied" and not interrupted:
            interrupted = True
            raise OSError("simulated journal interruption after sidecar replacement")
        return real_append(path, record)

    monkeypatch.setattr(delivery_base, "_append_journal", interrupt_applied_record_once)

    partial = apply_delivery(plan)
    assert partial.status == "partial"
    assert _sidecar(source, asset).is_file()

    resumed = apply_delivery(plan)
    assert resumed.status == "complete"
    assert resumed.outcomes[0].status == "skipped"

    rollback = rollback_delivery(resumed)

    assert rollback.status == "rolled_back"
    assert not _sidecar(source, asset).exists()


def test_resume_discards_only_an_unterminated_journal_tail(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    asset = _write_raw(source, "truncated-journal.nef", b"truncated-journal")
    plan = _plan(source, output, [asset], [SelectionTier.SELECT])
    first = apply_delivery(plan)
    journal = output / (plan.journal_path or "")
    with journal.open("ab") as handle:
        handle.write(b'{"status":"appl')

    resumed = apply_delivery(plan)

    assert first.status == resumed.status == "complete"
    assert resumed.outcomes[0].status == "skipped"
    assert journal.read_bytes().endswith(b"\n")


def test_resume_normalizes_a_complete_journal_record_without_newline(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    asset = _write_raw(source, "missing-newline.cr2", b"missing-newline")
    plan = _plan(source, output, [asset], [SelectionTier.TOP])
    first = apply_delivery(plan)
    journal = output / (plan.journal_path or "")
    journal.write_bytes(journal.read_bytes().removesuffix(b"\n"))

    resumed = apply_delivery(plan)

    assert first.status == resumed.status == "complete"
    assert resumed.outcomes[0].status == "skipped"
    assert journal.read_bytes().endswith(b"\n")


def test_previous_receipt_updates_owned_rating_and_removes_only_owned_keywords(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    asset = _write_raw(source, "owned.cr2", b"owned")
    sidecar = _sidecar(source, asset)
    sidecar.write_bytes(_xmp_with_keywords("user-keyword"))

    first = apply_delivery(
        _plan(source, output, [asset], [SelectionTier.TOP])
    )
    second = apply_delivery(
        _plan(source, output, [asset], [SelectionTier.SELECT], previous=first)
    )
    after_select = read_xmp(sidecar)
    removed = apply_delivery(
        _plan(source, output, [asset], [SelectionTier.NONE], previous=second)
    )
    after_removal = read_xmp(sidecar)

    assert first.status == second.status == removed.status == "complete"
    assert after_select["rating"] == 4
    assert after_select["keywords"] == ["user-keyword", "silverloupe:select"]
    assert after_removal["rating"] is None
    assert after_removal["keywords"] == ["user-keyword"]


def test_rollback_restores_preexisting_sidecar_and_removes_created_sidecar(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    existing = _write_raw(source, "existing.cr2", b"existing")
    created = _write_raw(source, "created.nef", b"created")
    existing_sidecar = _sidecar(source, existing)
    original = _xmp_with_keywords("user-keyword")
    existing_sidecar.write_bytes(original)
    receipt = apply_delivery(
        _plan(
            source,
            output,
            [existing, created],
            [SelectionTier.TOP, SelectionTier.SELECT],
        )
    )

    rollback = rollback_delivery(receipt)

    assert rollback.status == "rolled_back"
    assert existing_sidecar.read_bytes() == original
    assert not _sidecar(source, created).exists()
    assert [outcome.status for outcome in rollback.outcomes] == ["applied", "applied"]


@pytest.mark.parametrize("conflict_kind", ["external-edit", "missing-backup"])
def test_rollback_preserves_conflicting_state(
    tmp_path: Path,
    conflict_kind: str,
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    asset = _write_raw(source, "conflict.cr2", b"conflict")
    sidecar = _sidecar(source, asset)
    sidecar.write_bytes(_xmp_with_keywords("baseline"))
    receipt = apply_delivery(_plan(source, output, [asset], [SelectionTier.TOP]))
    if conflict_kind == "external-edit":
        sidecar.write_bytes(_xmp_with_keywords("external-edit"))
    else:
        backup = Path(receipt.output_root or "") / receipt.backups[0]
        backup.unlink()

    rollback = rollback_delivery(receipt)

    assert rollback.status == "partial"
    assert rollback.outcomes[0].status == "skipped"
    if conflict_kind == "external-edit":
        assert read_xmp(sidecar)["keywords"] == ["external-edit"]
        assert rollback.outcomes[0].conflict["reason"] == "postimage_changed"
    else:
        assert read_xmp(sidecar)["keywords"] == ["baseline", "silverloupe:top"]
        assert rollback.outcomes[0].conflict["reason"] == "backup_missing"
def test_delivery_cli_round_trip_supports_unicode_paths_and_repeated_apply(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "исходники с пробелом"
    output = tmp_path / "результат с пробелом"
    source.mkdir()
    asset = _write_raw(source, "кадр.cr2", b"raw")
    _write_completed_session(output, source, [asset])

    assert main(["delivery-plan", str(output), "--target", "xmp-sidecar"]) == 0
    plan_summary = json.loads(capsys.readouterr().out)
    assert plan_summary["status"] == "ready"
    assert Path(plan_summary["plan"]) == output / "delivery-plan.json"

    assert main(["delivery-apply", str(output)]) == 0
    first_summary = json.loads(capsys.readouterr().out)
    assert first_summary["status"] == "complete"
    assert read_xmp(_sidecar(source, asset))["rating"] == 5

    assert main(["delivery-apply", str(output)]) == 0
    repeated_summary = json.loads(capsys.readouterr().out)
    assert repeated_summary["applied"] == 0
    assert repeated_summary["skipped"] == 1

    assert main(["delivery-rollback", str(output)]) == 0
    rollback_summary = json.loads(capsys.readouterr().out)
    assert rollback_summary["status"] == "rolled_back"
    assert not _sidecar(source, asset).exists()


def test_delivery_plan_cli_requires_completed_curation_and_explicit_target(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    asset = _write_raw(source, "frame.nef", b"raw")
    _write_completed_session(output, source, [asset])
    state = RunState.model_validate_json((output / "run-state.json").read_text())
    (output / "run-state.json").write_text(
        state.model_copy(update={"status": "partial"}).model_dump_json(indent=2) + "\n"
    )

    assert main(["delivery-plan", str(output), "--target", "xmp-sidecar"]) == 2
    assert "completed" in capsys.readouterr().err
    assert not (output / "delivery-plan.json").exists()

    with pytest.raises(SystemExit):
        main(["delivery-plan", str(output)])


def test_delivery_cli_report_only_and_malformed_artifacts_fail_closed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    asset = _write_raw(source, "frame.cr2", b"raw")
    _write_completed_session(output, source, [asset])

    assert main(
        ["delivery-plan", str(output), "--target", "lightroom-classic-native"]
    ) == 0
    report_only = json.loads(capsys.readouterr().out)
    assert report_only["status"] == "report_only"
    assert report_only["conflicts"] == 1

    (output / "delivery-plan.json").write_text("not-json", encoding="utf-8")
    assert main(["delivery-apply", str(output)]) == 2
    assert "invalid delivery-plan.json" in capsys.readouterr().err

    (output / "delivery-receipt.json").write_text("not-json", encoding="utf-8")
    assert main(["delivery-rollback", str(output)]) == 2
    assert "invalid delivery-receipt.json" in capsys.readouterr().err


def test_delivery_cli_returns_partial_for_changed_preimage_and_rollback_conflict(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    asset = _write_raw(source, "frame.cr2", b"raw")
    _write_completed_session(output, source, [asset])
    assert main(["delivery-plan", str(output), "--target", "xmp-sidecar"]) == 0
    capsys.readouterr()
    (source / asset.relative_path).write_bytes(b"changed")

    assert main(["delivery-apply", str(output)]) == 3
    partial = json.loads(capsys.readouterr().out)
    assert partial["status"] == "partial"
    assert partial["failed"] == 1

    (source / asset.relative_path).write_bytes(b"raw")
    assert main(["delivery-plan", str(output), "--target", "xmp-sidecar"]) == 0
    capsys.readouterr()
    assert main(["delivery-apply", str(output)]) == 0
    capsys.readouterr()
    _sidecar(source, asset).write_bytes(_xmp_with_keywords("external"))
    assert main(["delivery-rollback", str(output)]) == 3
    rollback = json.loads(capsys.readouterr().out)
    assert rollback["status"] == "partial"
    assert rollback["conflicts"] == 1


def test_delivery_cli_rejects_symlinked_owned_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    outside = tmp_path / "outside"
    source.mkdir()
    outside.mkdir()
    asset = _write_raw(source, "frame.cr2", b"raw")
    _write_completed_session(output, source, [asset])
    (output / "delivery").symlink_to(outside, target_is_directory=True)

    assert main(["delivery-plan", str(output), "--target", "xmp-sidecar"]) == 2
    assert "symlink" in capsys.readouterr().err
    assert not (outside / "delivery-plan.json").exists()
