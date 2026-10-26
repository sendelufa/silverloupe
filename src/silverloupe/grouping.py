"""Conservative grouping of captures, bursts, and protected composite sets."""

from __future__ import annotations

import hashlib
import json
import math
import warnings
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
    raw = config.budget.get("grouping_policy", {})
    if not isinstance(raw, dict):
        raise ValueError("grouping_policy must be an object")

    policy = dict(raw)
    max_neighbors = policy.get("max_neighbors", _DEFAULT_MAX_NEIGHBORS)
    if isinstance(max_neighbors, bool) or not isinstance(max_neighbors, int) or max_neighbors < 1:
        raise ValueError("grouping_policy.max_neighbors must be an integer >= 1")
    policy["max_neighbors"] = max_neighbors

    numeric_limits = {
        "max_time_seconds": (_DEFAULT_MAX_SECONDS, 0.0, None),
        "burst_seconds": (_DEFAULT_BURST_SECONDS, 0.0, None),
        "visual_similarity": (_DEFAULT_VISUAL_SIMILARITY, 0.0, 1.0),
    }
    for name, (default, minimum, maximum) in numeric_limits.items():
        value = policy.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"grouping_policy.{name} must be a finite number")
        normalized = float(value)
        if not math.isfinite(normalized) or normalized < minimum:
            raise ValueError(f"grouping_policy.{name} must be a finite number >= {minimum:g}")
        if maximum is not None and normalized > maximum:
            raise ValueError(
                f"grouping_policy.{name} must be a finite number between {minimum:g} and {maximum:g}"
            )
        policy[name] = normalized
    return policy


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


def _time_domain(value: datetime) -> str:
    return "aware" if value.tzinfo is not None and value.utcoffset() is not None else "naive"


def _time_delta_seconds(first: datetime, second: datetime) -> float | None:
    if _time_domain(first) != _time_domain(second):
        return None
    return abs((first - second).total_seconds())


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
    max_seconds = policy["max_time_seconds"]
    max_neighbors = policy["max_neighbors"]
    buckets: dict[tuple[str, str], list[tuple[datetime, Asset]]] = defaultdict(list)
    for asset in manifest.assets:
        captured = _capture_time(asset)
        camera = _camera(asset)
        if captured is not None and camera is not None:
            buckets[(camera, _time_domain(captured))].append((captured, asset))

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
            flattened = getattr(gray, "get_flattened_data", None)
            if flattened is not None:
                pixels = list(flattened())
            else:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    pixels = list(gray.getdata())
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
        delta: float | None = None
        if raw_time is not None and raster_time is not None:
            delta = _time_delta_seconds(raw_time, raster_time)
            if delta is None:
                groups.append(
                    _group(
                        "uncertain",
                        [raw.asset_id, raster.asset_id],
                        confidence_kind="unknown",
                        confidence_value=None,
                        protected_set=False,
                        evidence=[
                            {"kind": "same_stem_incomparable_timestamps", "stem": stem}
                        ],
                    )
                )
                continue
            if delta > 1.5:
                continue
        complete_identity = all(
            value is not None
            for value in (raw_camera, raster_camera, raw_time, raster_time)
        )
        evidence = (
            [{"kind": "capture_identity", "stem": stem, "time_delta_seconds": delta}]
            if complete_identity
            else [{"kind": "unique_same_stem_companion", "stem": stem}]
        )
        groups.append(
            _group(
                "raw_jpeg_companion",
                [raw.asset_id, raster.asset_id],
                confidence_kind="heuristic",
                confidence_value=0.98 if complete_identity else 0.9,
                protected_set=False,
                evidence=evidence,
            )
        )
    return groups


def _resolve_composite_conflicts(groups: list[Group]) -> list[Group]:
    companion_adjacency: dict[str, set[str]] = defaultdict(set)
    for group in groups:
        if group.type != "raw_jpeg_companion":
            continue
        for member in group.member_ids:
            companion_adjacency[member].update(set(group.member_ids) - {member})

    def expanded_members(group: Group) -> set[str]:
        expanded = set(group.member_ids)
        pending = list(expanded)
        while pending:
            member = pending.pop()
            for companion in companion_adjacency.get(member, set()) - expanded:
                expanded.add(companion)
                pending.append(companion)
        return expanded

    protected = [
        (index, group, expanded_members(group))
        for index, group in enumerate(groups)
        if group.protected_set and group.type in _COMPOSITE_TYPES
    ]
    conflicted: set[int] = set()
    for position, (first_index, _first, first_members) in enumerate(protected):
        for second_index, _second, second_members in protected[position + 1 :]:
            if first_members & second_members:
                conflicted.update({first_index, second_index})
    resolved: list[Group] = []
    for index, group in enumerate(groups):
        if index not in conflicted:
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
    visual_threshold = policy["visual_similarity"]
    burst_seconds = policy["burst_seconds"]
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
        exposure_values = [_number(asset.exif.get("ExposureTime")) for asset in component_assets]
        exposures = {value for value in exposure_values if value is not None and value > 0}
        focus_values = [_number(asset.exif.get("FocusDistance")) for asset in component_assets]
        focus_distances = {value for value in focus_values if value is not None and value > 0}
        complete_exposures = len(exposures) >= 3 and len(exposures) == len(component_assets)
        complete_focus = len(focus_distances) >= 3 and len(focus_distances) == len(component_assets)
        exposure_span = max(exposures) / min(exposures) if complete_exposures else None
        focus_span = max(focus_distances) / min(focus_distances) if complete_focus else None
        if len(members) >= 3 and exposure_span is not None and exposure_span >= 4.0:
            groups.append(
                _group(
                    "hdr",
                    members,
                    confidence_kind="measured",
                    confidence_value=0.9,
                    protected_set=True,
                    evidence=[
                        {
                            "kind": "exposure_bracket",
                            "distinct_exposures": len(exposures),
                            "span_ratio": round(exposure_span, 4),
                        }
                    ],
                )
            )
            protected_members.update(members)
        elif len(members) >= 3 and focus_span is not None and focus_span >= 2.0:
            groups.append(
                _group(
                    "focus_stack",
                    members,
                    confidence_kind="measured",
                    confidence_value=0.9,
                    protected_set=True,
                    evidence=[
                        {
                            "kind": "focus_bracket",
                            "distinct_distances": len(focus_distances),
                            "span_ratio": round(focus_span, 4),
                        }
                    ],
                )
            )
            protected_members.update(members)
        elif len(members) >= 3 and (complete_exposures or complete_focus):
            candidate_type = "hdr" if complete_exposures else "focus_stack"
            groups.append(
                _group(
                    "uncertain",
                    members,
                    confidence_kind="unknown",
                    confidence_value=None,
                    protected_set=False,
                    evidence=[
                        {
                            "kind": "weak_composite_candidate",
                            "candidate_type": candidate_type,
                            "span_ratio": round(
                                exposure_span if complete_exposures else focus_span or 0.0, 4
                            ),
                        }
                    ],
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
    group_ids = [group.group_id for group in groups]
    duplicate_group_ids = sorted(
        group_id for group_id in set(group_ids) if group_ids.count(group_id) > 1
    )
    if duplicate_group_ids:
        raise ValueError(f"duplicate group_id values: {duplicate_group_ids}")

    groups_by_asset: dict[str, list[Group]] = defaultdict(list)
    for group in groups:
        for member in group.member_ids:
            if member not in assets_by_id:
                raise ValueError(f"group {group.group_id!r} references unknown asset {member!r}")
            groups_by_asset[member].append(group)

    companion_adjacency: dict[str, set[str]] = defaultdict(set)
    for group in groups:
        if group.type != "raw_jpeg_companion":
            continue
        for member in group.member_ids:
            companion_adjacency[member].update(set(group.member_ids) - {member})

    expanded_protected: dict[str, set[str]] = {}
    for group in groups:
        if not group.protected_set or group.type not in _COMPOSITE_TYPES:
            continue
        members = set(group.member_ids)
        pending = list(members)
        while pending:
            member = pending.pop()
            for companion in companion_adjacency.get(member, set()) - members:
                members.add(companion)
                pending.append(companion)
        expanded_protected[group.group_id] = members

    protected_memberships: dict[str, str] = {}
    for group_id, members in expanded_protected.items():
        for member in members:
            previous = protected_memberships.get(member)
            if previous is not None and previous != group_id:
                raise ValueError(
                    f"overlapping protected groups must be resolved before selection items: "
                    f"{previous!r} and {group_id!r} share {member!r}"
                )
            protected_memberships[member] = group_id

    items: list[SelectionItem] = []
    consumed: set[str] = set()
    raw_groups = [group for group in groups if group.type == "raw_jpeg_companion"]
    protected = [group for group in groups if group.protected_set and group.type in _COMPOSITE_TYPES]
    for group in sorted(protected, key=lambda value: value.group_id):
        members = expanded_protected[group.group_id]
        if members & consumed:
            raise ValueError(
                f"overlapping protected groups must be resolved before selection items: "
                f"{group.group_id!r} reuses consumed assets"
            )
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


def _merge_manual_groups(
    generated: list[Group], previous: list[Group], manifest: Manifest
) -> tuple[list[Group], bool]:
    manual = [group for group in previous if group.confidence_kind == "manual"]
    if not manual:
        return generated, False

    known = {asset.asset_id for asset in manifest.assets}
    reconciled: list[Group] = []
    for group in manual:
        members = sorted(set(group.member_ids) & known)
        if not members:
            continue
        minimum = 3 if group.type == "hdr" else 2
        if group.protected_set and group.type in _COMPOSITE_TYPES and len(members) < minimum:
            reconciled.append(
                group.model_copy(
                    update={
                        "type": "uncertain",
                        "member_ids": members,
                        "protected_set": False,
                        "evidence": [
                            *group.evidence,
                            {"kind": "manual_group_incomplete", "candidate_type": group.type},
                        ],
                    }
                )
            )
        else:
            reconciled.append(group.model_copy(update={"member_ids": members}))

    manual_members = {member for group in reconciled for member in group.member_ids}
    retained_generated = [
        group
        for group in generated
        if group.type == "byte_duplicate" or not (set(group.member_ids) & manual_members)
    ]
    merged = [*retained_generated, *reconciled]
    return sorted(merged, key=lambda group: (group.member_ids[0], group.type, group.group_id)), bool(
        reconciled
    )


def group_prepared(
    output: Path, *, reuse_groups: bool = False, reset_manual_groups: bool = False
) -> dict[str, Any]:
    """Group a prepared session and atomically write its contract artifacts."""

    output = output.expanduser().resolve()
    validate_output_layout(output)
    config = load_run_config(output)
    _policy(config)
    manifest = _read_manifest(output)
    manual_preserved = False
    if reuse_groups:
        groups = _read_groups(output)
    else:
        groups = group_assets(manifest, config)
        previous_path = output / "groups.json"
        if previous_path.is_file() and not reset_manual_groups:
            groups, manual_preserved = _merge_manual_groups(groups, _read_groups(output), manifest)
    items = build_selection_items(manifest, groups)
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
        "validation": (
            "manual-groups-reused"
            if reuse_groups
            else "manual-groups-preserved"
            if manual_preserved
            else "synthetic-or-user-review-required"
        ),
        "real_photo_validation": "unavailable",
    }
    artifacts = {
        output / "groups.json": json.dumps(
            [group.model_dump(mode="json") for group in groups],
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        output / "selection-items.json": json.dumps(
            [item.model_dump(mode="json") for item in items],
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        output / "grouping-report.json": json.dumps(
            report, ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n",
    }
    for path, contents in artifacts.items():
        atomic_write_text(path, contents)
    return report
