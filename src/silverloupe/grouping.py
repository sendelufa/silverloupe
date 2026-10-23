"""Conservative grouping of captures, bursts, and protected composite sets."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageOps, UnidentifiedImageError

from silverloupe.contracts import Asset, Group, Manifest, RunConfig, SelectionItem
from silverloupe.previews import RAW_EXTENSIONS, validate_output_layout
from silverloupe.state import atomic_write_text, load_run_config


_COMPOSITE_TYPES = {"hdr", "panorama", "focus_stack"}
_DEFAULT_MAX_SECONDS = 5.0
_DEFAULT_BURST_SECONDS = 2.0
_DEFAULT_MAX_NEIGHBORS = 24
_DEFAULT_VISUAL_SIMILARITY = 0.85


def _stable_id(prefix: str, kind: str, members: Iterable[str]) -> str:
    payload = json.dumps([kind, *sorted(members)], separators=(",", ":")).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:20]}"


def _policy(config: RunConfig) -> dict[str, Any]:
    value = config.budget.get("grouping_policy", {})
    return value if isinstance(value, dict) else {}


def _capture_time(asset: Asset) -> datetime | None:
    value = next(
        (
            asset.exif.get(key)
            for key in ("DateTimeOriginal", "DateTimeDigitized", "DateTime")
            if asset.exif.get(key)
        ),
        None,
    )
    if not isinstance(value, str):
        return None
    for pattern in ("%Y:%m:%d %H:%M:%S.%f", "%Y:%m:%d %H:%M:%S"):
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _camera(asset: Asset) -> str | None:
    serial = asset.exif.get("BodySerialNumber") or asset.exif.get("SerialNumber")
    make = asset.exif.get("Make")
    model = asset.exif.get("Model")
    parts = [str(value).strip().casefold() for value in (make, model, serial) if value]
    return "|".join(parts) if parts else None


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            numerator, denominator = float(value[0]), float(value[1])
            return numerator / denominator if denominator else None
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        try:
            if "/" in value:
                numerator, denominator = value.split("/", 1)
                return float(numerator) / float(denominator)
            return float(value)
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    return None


def _candidate_pairs(manifest: Manifest, config: RunConfig) -> list[tuple[Asset, Asset, float]]:
    policy = _policy(config)
    max_seconds = float(policy.get("max_time_seconds", _DEFAULT_MAX_SECONDS))
    max_neighbors = max(1, int(policy.get("max_neighbors", _DEFAULT_MAX_NEIGHBORS)))
    buckets: dict[str, list[tuple[datetime, Asset]]] = defaultdict(list)
    for asset in manifest.assets:
        captured = _capture_time(asset)
        camera = _camera(asset)
        if captured is not None and camera is not None:
            buckets[camera].append((captured, asset))

    pairs: list[tuple[Asset, Asset, float]] = []
    for entries in buckets.values():
        entries.sort(key=lambda entry: (entry[0], entry[1].asset_id))
        for index, (captured, asset) in enumerate(entries):
            upper = min(len(entries), index + max_neighbors + 1)
            for other_time, other in entries[index + 1 : upper]:
                delta = (other_time - captured).total_seconds()
                if delta > max_seconds:
                    break
                pairs.append((asset, other, delta))
    return pairs


def candidate_pair_count(manifest: Manifest, config: RunConfig) -> int:
    """Return the number of indexed temporal candidates for diagnostics."""

    return len(_candidate_pairs(manifest, config))


def _preview_path(asset: Asset, config: RunConfig) -> Path | None:
    provenance = asset.preview_provenance or {}
    relative_value = provenance.get("path")
    if not isinstance(relative_value, str):
        return None
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    output = Path(config.output_root).expanduser().resolve()
    candidate = output / relative
    if candidate.is_symlink() or not candidate.resolve().is_relative_to(output):
        return None
    return candidate if candidate.is_file() else None


def _average_hash(asset: Asset, config: RunConfig, cache: dict[str, int | None]) -> int | None:
    if asset.asset_id in cache:
        return cache[asset.asset_id]
    path = _preview_path(asset, config)
    if path is None:
        cache[asset.asset_id] = None
        return None
    try:
        with Image.open(path) as opened:
            gray = ImageOps.grayscale(opened).resize((8, 8), Image.Resampling.LANCZOS)
            pixels = list(gray.get_flattened_data())
    except (Image.DecompressionBombError, OSError, UnidentifiedImageError):
        cache[asset.asset_id] = None
        return None
    mean = sum(pixels) / len(pixels)
    value = sum(1 << index for index, pixel in enumerate(pixels) if pixel >= mean)
    cache[asset.asset_id] = value
    return value


def _visual_similarity(
    first: Asset, second: Asset, config: RunConfig, cache: dict[str, int | None]
) -> float | None:
    first_hash = _average_hash(first, config, cache)
    second_hash = _average_hash(second, config, cache)
    if first_hash is None or second_hash is None:
        return None
    return 1.0 - ((first_hash ^ second_hash).bit_count() / 64.0)


def _components(edges: Iterable[tuple[str, str]]) -> list[list[str]]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for first, second in edges:
        adjacency[first].add(second)
        adjacency[second].add(first)
    components: list[list[str]] = []
    remaining = set(adjacency)
    while remaining:
        root = min(remaining)
        stack = [root]
        members: set[str] = set()
        while stack:
            current = stack.pop()
            if current in members:
                continue
            members.add(current)
            stack.extend(adjacency[current] - members)
        remaining -= members
        components.append(sorted(members))
    return components


def _group(
    group_type: str,
    members: Iterable[str],
    *,
    confidence_kind: str,
    confidence_value: float | None,
    protected_set: bool,
    evidence: list[dict[str, Any]],
) -> Group:
    member_ids = sorted(set(members))
    return Group(
        group_id=_stable_id("group", group_type, member_ids),
        type=group_type,
        member_ids=member_ids,
        confidence_kind=confidence_kind,
        confidence_value=confidence_value,
        protected_set=protected_set,
        evidence=evidence,
    )


def _sequence_types(asset: Asset) -> list[str]:
    value = asset.existing_metadata.get("sequence_type", asset.exif.get("SequenceType"))
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = [str(item) for item in value]
    else:
        values = []
    return sorted({value.strip().casefold() for value in values if value.strip().casefold() in _COMPOSITE_TYPES})


def _explicit_composites(manifest: Manifest) -> list[Group]:
    buckets: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for asset in manifest.assets:
        sequence_id = asset.existing_metadata.get("sequence_id", asset.exif.get("SequenceID"))
        if not sequence_id:
            continue
        camera = _camera(asset) or "unknown-camera"
        for sequence_type in _sequence_types(asset):
            buckets[(sequence_type, str(sequence_id), camera)].append(asset.asset_id)
    groups: list[Group] = []
    for (sequence_type, sequence_id, _camera_id), members in sorted(buckets.items()):
        minimum = 3 if sequence_type == "hdr" else 2
        if len(set(members)) < minimum:
            continue
        groups.append(
            _group(
                sequence_type,
                members,
                confidence_kind="heuristic",
                confidence_value=0.95,
                protected_set=True,
                evidence=[{"kind": "sequence_metadata", "sequence_id": sequence_id}],
            )
        )
    return groups


def _raw_jpeg_groups(manifest: Manifest) -> list[Group]:
    candidates: dict[tuple[str, str], list[Asset]] = defaultdict(list)
    for asset in manifest.assets:
        path = Path(asset.relative_path)
        candidates[(path.parent.as_posix().casefold(), path.stem.casefold())].append(asset)
    groups: list[Group] = []
    for (_parent, stem), assets in sorted(candidates.items()):
        raw_assets = [asset for asset in assets if Path(asset.relative_path).suffix.lower() in RAW_EXTENSIONS]
        raster_assets = [asset for asset in assets if Path(asset.relative_path).suffix.lower() not in RAW_EXTENSIONS]
        if not raw_assets or not raster_assets:
            continue
        if len(raw_assets) != 1 or len(raster_assets) != 1:
            groups.append(
                _group(
                    "uncertain",
                    [asset.asset_id for asset in assets],
                    confidence_kind="unknown",
                    confidence_value=None,
                    protected_set=False,
                    evidence=[{"kind": "ambiguous_raw_jpeg_stem", "stem": stem}],
                )
            )
            continue
        raw, raster = raw_assets[0], raster_assets[0]
        raw_camera, raster_camera = _camera(raw), _camera(raster)
        raw_time, raster_time = _capture_time(raw), _capture_time(raster)
        if raw_camera and raster_camera and raw_camera != raster_camera:
            continue
        if raw_time and raster_time:
            delta = abs((raw_time - raster_time).total_seconds())
            if delta > 1.5:
                continue
        elif raw_camera is None or raster_camera is None:
            groups.append(
                _group(
                    "uncertain",
                    [raw.asset_id, raster.asset_id],
                    confidence_kind="unknown",
                    confidence_value=None,
                    protected_set=False,
                    evidence=[{"kind": "same_stem_insufficient_metadata", "stem": stem}],
                )
            )
            continue
        else:
            delta = None
        groups.append(
            _group(
                "raw_jpeg_companion",
                [raw.asset_id, raster.asset_id],
                confidence_kind="heuristic",
                confidence_value=0.98,
                protected_set=False,
                evidence=[{"kind": "capture_identity", "stem": stem, "time_delta_seconds": delta}],
            )
        )
    return groups


def _resolve_composite_conflicts(groups: list[Group]) -> list[Group]:
    protected = [group for group in groups if group.protected_set]
    conflicted: set[str] = set()
    for index, first in enumerate(protected):
        for second in protected[index + 1 :]:
            if set(first.member_ids) & set(second.member_ids):
                conflicted.update({first.group_id, second.group_id})
    resolved: list[Group] = []
    for group in groups:
        if group.group_id not in conflicted:
            resolved.append(group)
            continue
        resolved.append(
            group.model_copy(
                update={
                    "type": "uncertain",
                    "protected_set": False,
                    "confidence_kind": "unknown",
                    "confidence_value": None,
                    "evidence": [
                        *group.evidence,
                        {"kind": "conflicting_composite", "candidate_type": group.type},
                    ],
                }
            )
        )
    return resolved


def group_assets(manifest: Manifest, config: RunConfig) -> list[Group]:
    """Return deterministic, non-exclusive relations without risky ambiguous merges."""

    validate_output_layout(Path(config.output_root))
    assets_by_id = {asset.asset_id: asset for asset in manifest.assets}
    groups: list[Group] = []

    by_digest: dict[str, list[str]] = defaultdict(list)
    for asset in manifest.assets:
        by_digest[asset.content_sha256].append(asset.asset_id)
    for digest, members in sorted(by_digest.items()):
        if len(members) > 1:
            groups.append(
                _group(
                    "byte_duplicate",
                    members,
                    confidence_kind="measured",
                    confidence_value=1.0,
                    protected_set=False,
                    evidence=[{"kind": "content_sha256", "value": digest}],
                )
            )

    groups.extend(_raw_jpeg_groups(manifest))
    explicit = _explicit_composites(manifest)
    groups.extend(explicit)
    explicit_members = {member for group in explicit for member in group.member_ids}

    policy = _policy(config)
    visual_threshold = float(policy.get("visual_similarity", _DEFAULT_VISUAL_SIMILARITY))
    burst_seconds = float(policy.get("burst_seconds", _DEFAULT_BURST_SECONDS))
    hash_cache: dict[str, int | None] = {}
    temporal = _candidate_pairs(manifest, config)
    measured_edges: list[tuple[str, str, float, float]] = []
    unknown_edges: list[tuple[str, str]] = []
    for first, second, delta in temporal:
        if first.asset_id in explicit_members or second.asset_id in explicit_members:
            continue
        if first.content_sha256 == second.content_sha256:
            continue
        similarity = _visual_similarity(first, second, config, hash_cache)
        if similarity is None:
            if delta <= burst_seconds:
                unknown_edges.append((first.asset_id, second.asset_id))
        elif similarity >= visual_threshold:
            measured_edges.append((first.asset_id, second.asset_id, delta, similarity))

    protected_members: set[str] = set(explicit_members)
    similar_components = _components((first, second) for first, second, _delta, _similarity in measured_edges)
    for members in similar_components:
        component_assets = [assets_by_id[member] for member in members]
        exposures = {_number(asset.exif.get("ExposureTime")) for asset in component_assets}
        exposures.discard(None)
        focus_distances = {_number(asset.exif.get("FocusDistance")) for asset in component_assets}
        focus_distances.discard(None)
        if len(members) >= 3 and len(exposures) >= 3:
            groups.append(
                _group(
                    "hdr",
                    members,
                    confidence_kind="measured",
                    confidence_value=0.9,
                    protected_set=True,
                    evidence=[{"kind": "exposure_bracket", "distinct_exposures": len(exposures)}],
                )
            )
            protected_members.update(members)
        elif len(members) >= 3 and len(focus_distances) >= 3:
            groups.append(
                _group(
                    "focus_stack",
                    members,
                    confidence_kind="measured",
                    confidence_value=0.9,
                    protected_set=True,
                    evidence=[{"kind": "focus_bracket", "distinct_distances": len(focus_distances)}],
                )
            )
            protected_members.update(members)

    burst_edges: list[tuple[str, str]] = []
    near_edges: list[tuple[str, str]] = []
    similarity_by_edge: dict[frozenset[str], float] = {}
    for first, second, delta, similarity in measured_edges:
        if first in protected_members or second in protected_members:
            continue
        edge = (first, second)
        similarity_by_edge[frozenset(edge)] = similarity
        if delta <= burst_seconds:
            burst_edges.append(edge)
        else:
            near_edges.append(edge)
    for group_type, edges in (("burst", burst_edges), ("near_duplicate", near_edges)):
        for members in _components(edges):
            edge_values = [
                similarity_by_edge[frozenset((first, second))]
                for first, second in edges
                if first in members and second in members
            ]
            groups.append(
                _group(
                    group_type,
                    members,
                    confidence_kind="measured",
                    confidence_value=min(edge_values) if edge_values else None,
                    protected_set=False,
                    evidence=[
                        {"kind": "candidate_window", "max_seconds": burst_seconds},
                        {"kind": "visual_similarity", "minimum": round(min(edge_values), 4)},
                    ],
                )
            )

    occupied_by_temporal = {member for group in groups if group.type in {"burst", "near_duplicate"} for member in group.member_ids}
    uncertain_edges = [
        (first, second)
        for first, second in unknown_edges
        if first not in protected_members
        and second not in protected_members
        and first not in occupied_by_temporal
        and second not in occupied_by_temporal
    ]
    for members in _components(uncertain_edges):
        groups.append(
            _group(
                "uncertain",
                members,
                confidence_kind="unknown",
                confidence_value=None,
                protected_set=False,
                evidence=[{"kind": "temporal_candidate_without_visual_evidence"}],
            )
        )

    groups = _resolve_composite_conflicts(groups)
    related = {member for group in groups for member in group.member_ids}
    for asset in manifest.assets:
        if asset.asset_id not in related:
            groups.append(
                _group(
                    "single",
                    [asset.asset_id],
                    confidence_kind="measured",
                    confidence_value=1.0,
                    protected_set=False,
                    evidence=[{"kind": "no_supported_relation"}],
                )
            )
    return sorted(groups, key=lambda group: (group.member_ids[0], group.type, group.group_id))


def _primary(members: list[str], assets_by_id: dict[str, Asset]) -> str:
    raw = [
        member
        for member in members
        if Path(assets_by_id[member].relative_path).suffix.lower() in RAW_EXTENSIONS
    ]
    return sorted(raw or members)[0]


def build_selection_items(manifest: Manifest, groups: list[Group]) -> list[SelectionItem]:
    """Build a lossless item partition; only trusted relations collapse assets."""

    assets_by_id = {asset.asset_id: asset for asset in manifest.assets}
    manifest_order = {asset.asset_id: index for index, asset in enumerate(manifest.assets)}
    groups_by_asset: dict[str, list[Group]] = defaultdict(list)
    for group in groups:
        for member in group.member_ids:
            if member not in assets_by_id:
                raise ValueError(f"group {group.group_id!r} references unknown asset {member!r}")
            groups_by_asset[member].append(group)

    protected_memberships: dict[str, str] = {}
    for group in groups:
        if not group.protected_set or group.type not in _COMPOSITE_TYPES:
            continue
        for member in group.member_ids:
            previous = protected_memberships.get(member)
            if previous is not None and previous != group.group_id:
                raise ValueError(
                    f"overlapping protected groups must be resolved before selection items: "
                    f"{previous!r} and {group.group_id!r} share {member!r}"
                )
            protected_memberships[member] = group.group_id

    items: list[SelectionItem] = []
    consumed: set[str] = set()
    raw_groups = [group for group in groups if group.type == "raw_jpeg_companion"]
    protected = [group for group in groups if group.protected_set and group.type in _COMPOSITE_TYPES]
    for group in sorted(protected, key=lambda value: value.group_id):
        members = set(group.member_ids)
        for companion in raw_groups:
            if members & set(companion.member_ids):
                members.update(companion.member_ids)
        if members & consumed:
            continue
        member_ids = sorted(members)
        attached_group_ids = {
            related.group_id for member in member_ids for related in groups_by_asset[member]
        }
        items.append(
            SelectionItem(
                item_id=_stable_id("item", "composite", member_ids),
                kind="composite",
                member_asset_ids=member_ids,
                primary_asset_id=_primary(sorted(group.member_ids), assets_by_id),
                required_asset_ids=sorted(group.member_ids),
                group_ids=sorted(attached_group_ids),
            )
        )
        consumed.update(member_ids)

    for group in sorted(raw_groups, key=lambda value: value.group_id):
        members = set(group.member_ids)
        if members & consumed:
            continue
        member_ids = sorted(members)
        associated_group_ids = {
            related.group_id for member in member_ids for related in groups_by_asset[member]
        }
        items.append(
            SelectionItem(
                item_id=_stable_id("item", "capture", member_ids),
                kind="capture",
                member_asset_ids=member_ids,
                primary_asset_id=_primary(member_ids, assets_by_id),
                group_ids=sorted(associated_group_ids),
            )
        )
        consumed.update(member_ids)

    for asset in manifest.assets:
        if asset.asset_id in consumed:
            continue
        group_ids = sorted(group.group_id for group in groups_by_asset.get(asset.asset_id, []))
        items.append(
            SelectionItem(
                item_id=_stable_id("item", "capture", [asset.asset_id]),
                kind="capture",
                member_asset_ids=[asset.asset_id],
                primary_asset_id=asset.asset_id,
                group_ids=group_ids,
            )
        )
        consumed.add(asset.asset_id)

    return sorted(items, key=lambda item: min(manifest_order[member] for member in item.member_asset_ids))


def _read_manifest(output: Path) -> Manifest:
    assets_path = output / "assets.jsonl"
    if not assets_path.is_file():
        raise FileNotFoundError(f"asset manifest does not exist: {assets_path}")
    assets: list[Asset] = []
    for line_number, line in enumerate(assets_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            assets.append(Asset.model_validate_json(line))
        except ValueError as error:
            raise ValueError(f"invalid assets.jsonl line {line_number}: {error}") from error
    return Manifest(assets=assets)


def _read_groups(output: Path) -> list[Group]:
    groups_path = output / "groups.json"
    if not groups_path.is_file():
        raise FileNotFoundError(f"group artifact does not exist: {groups_path}")
    try:
        value = json.loads(groups_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"invalid groups.json: {error}") from error
    if not isinstance(value, list):
        raise ValueError("invalid groups.json: expected a JSON array")
    return [Group.model_validate(item) for item in value]


def group_prepared(output: Path, *, reuse_groups: bool = False) -> dict[str, Any]:
    """Group a prepared session and atomically write its contract artifacts."""

    output = output.expanduser().resolve()
    validate_output_layout(output)
    config = load_run_config(output)
    manifest = _read_manifest(output)
    groups = _read_groups(output) if reuse_groups else group_assets(manifest, config)
    items = build_selection_items(manifest, groups)
    atomic_write_text(
        output / "groups.json",
        json.dumps([group.model_dump(mode="json") for group in groups], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    atomic_write_text(
        output / "selection-items.json",
        json.dumps([item.model_dump(mode="json") for item in items], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    counts: dict[str, int] = defaultdict(int)
    for group in groups:
        counts[group.type] += 1
    report = {
        "schema_version": 1,
        "assets": len(manifest.assets),
        "groups": len(groups),
        "selection_items": len(items),
        "group_counts": dict(sorted(counts.items())),
        "candidate_comparisons": candidate_pair_count(manifest, config),
        "review_group_ids": [group.group_id for group in groups if group.type == "uncertain"],
        "validation": "manual-groups-reused" if reuse_groups else "synthetic-or-user-review-required",
        "real_photo_validation": "unavailable",
    }
    atomic_write_text(
        output / "grouping-report.json",
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return report
