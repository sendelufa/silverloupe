"""Comparison-batch construction for grouped assets."""

from __future__ import annotations

from silverloupe.contracts import Group, Manifest


_REVIEWABLE_TYPES = {"burst", "near_duplicate", "uncertain", "hdr", "panorama", "focus_stack"}


def make_comparison_batches(groups: list[Group], manifest: Manifest) -> list[dict]:
    """Return deterministic side-by-side batches with stable asset references."""

    assets = {asset.asset_id: asset for asset in manifest.assets}
    batches: list[dict] = []
    for group in sorted(groups, key=lambda value: value.group_id):
        if group.type not in _REVIEWABLE_TYPES:
            continue
        missing = set(group.member_ids) - set(assets)
        if missing:
            raise ValueError(f"group {group.group_id!r} references unknown assets: {sorted(missing)}")
        batches.append(
            {
                "group_id": group.group_id,
                "group_type": group.type,
                "asset_ids": list(group.member_ids),
                "preview_paths": [
                    (assets[asset_id].preview_provenance or {}).get("path")
                    for asset_id in group.member_ids
                ],
                "protected_set": group.protected_set,
                "evidence": group.evidence,
            }
        )
    return batches
