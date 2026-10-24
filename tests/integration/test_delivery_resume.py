from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import silverloupe.delivery.base as delivery_base
from silverloupe.contracts import (
    Asset,
    Decision,
    DecisionStatus,
    Manifest,
    ProposedChange,
    SelectionItem,
    SelectionTier,
)
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
