"""Semantic XMP sidecar merge and staged file replacement."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from silverloupe.contracts import AssetMapping, DeliveryPlan, ProposedChange


NS_X = "adobe:ns:meta/"
NS_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
NS_XMP = "http://ns.adobe.com/xap/1.0/"
NS_DC = "http://purl.org/dc/elements/1.1/"

for prefix, namespace in (
    ("x", NS_X),
    ("rdf", NS_RDF),
    ("xmp", NS_XMP),
    ("dc", NS_DC),
):
    ET.register_namespace(prefix, namespace)


@dataclass(frozen=True)
class StagedXmp:
    plan_id: str
    asset_id: str
    source_path: Path
    target_path: Path
    staged_path: Path
    backup_path: Path | None
    source_sha256: str
    target_existed: bool
    target_sha256: str | None
    staged_sha256: str
    readback: dict[str, Any]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_unsafe_xml(data: bytes) -> None:
    lowered = data.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ValueError("XMP packet must not contain DOCTYPE or entity declarations")


def _new_root() -> ET.Element:
    root = ET.Element(f"{{{NS_X}}}xmpmeta")
    rdf = ET.SubElement(root, f"{{{NS_RDF}}}RDF")
    ET.SubElement(rdf, f"{{{NS_RDF}}}Description")
    return root


def _parse(data: bytes | None) -> ET.Element:
    if data is None:
        return _new_root()
    _reject_unsafe_xml(data)
    try:
        return ET.fromstring(data)
    except ET.ParseError as error:
        raise ValueError(f"invalid XMP packet: {error}") from error


def _descriptions(root: ET.Element) -> list[ET.Element]:
    rdf = root if root.tag == f"{{{NS_RDF}}}RDF" else root.find(f".//{{{NS_RDF}}}RDF")
    if rdf is None:
        rdf = ET.SubElement(root, f"{{{NS_RDF}}}RDF")
    descriptions = list(rdf.findall(f"{{{NS_RDF}}}Description"))
    if not descriptions:
        descriptions.append(ET.SubElement(rdf, f"{{{NS_RDF}}}Description"))
    return descriptions


def _scalar(description: ET.Element, namespace: str, name: str) -> str | None:
    key = f"{{{namespace}}}{name}"
    if key in description.attrib:
        return description.attrib[key]
    element = description.find(key)
    if element is not None and element.text is not None:
        return element.text
    return None


def _set_scalar(
    root: ET.Element,
    namespace: str,
    name: str,
    value: Any,
) -> None:
    key = f"{{{namespace}}}{name}"
    descriptions = _descriptions(root)
    description = next(
        (
            candidate
            for candidate in descriptions
            if key in candidate.attrib or candidate.find(key) is not None
        ),
        descriptions[0],
    )
    for candidate in descriptions:
        for element in list(candidate.findall(key)):
            candidate.remove(element)
        if candidate is not description:
            candidate.attrib.pop(key, None)
    if value is None:
        description.attrib.pop(key, None)
    else:
        description.set(key, str(value))


def _keywords(description: ET.Element) -> list[str]:
    subject = description.find(f"{{{NS_DC}}}subject")
    if subject is None:
        return []
    bag = subject.find(f"{{{NS_RDF}}}Bag")
    if bag is None:
        return []
    return [
        element.text
        for element in bag.findall(f"{{{NS_RDF}}}li")
        if element.text is not None
    ]


def _all_keywords(root: ET.Element) -> list[str]:
    values: list[str] = []
    for description in _descriptions(root):
        values.extend(_keywords(description))
    return list(dict.fromkeys(values))


def _set_keywords(root: ET.Element, values: list[str]) -> None:
    descriptions = _descriptions(root)
    description = next(
        (
            candidate
            for candidate in descriptions
            if candidate.find(f"{{{NS_DC}}}subject") is not None
        ),
        descriptions[0],
    )
    for candidate in descriptions:
        for subject in list(candidate.findall(f"{{{NS_DC}}}subject")):
            candidate.remove(subject)
    subject = ET.SubElement(description, f"{{{NS_DC}}}subject")
    bag = ET.SubElement(subject, f"{{{NS_RDF}}}Bag")
    for value in values:
        element = ET.SubElement(bag, f"{{{NS_RDF}}}li")
        element.text = value


def _values(root: ET.Element) -> dict[str, Any]:
    descriptions = _descriptions(root)
    rating_text = next(
        (
            value
            for description in descriptions
            if (value := _scalar(description, NS_XMP, "Rating")) is not None
        ),
        None,
    )
    label = next(
        (
            value
            for description in descriptions
            if (value := _scalar(description, NS_XMP, "Label")) is not None
        ),
        None,
    )
    try:
        rating: int | None = int(rating_text) if rating_text is not None else None
    except ValueError:
        rating = None
    return {
        "rating": rating,
        "label": label,
        "keywords": _all_keywords(root),
    }


def read_xmp(path: Path) -> dict[str, Any]:
    """Read the Silverloupe-owned XMP fields without changing the packet."""

    if not path.is_file():
        return {"rating": None, "label": None, "keywords": []}
    return _values(_parse(path.read_bytes()))


def merge_xmp(
    original: bytes | None,
    changes: list[ProposedChange],
    ownership: dict[str, Any],
) -> tuple[bytes, dict[str, Any]]:
    """Merge only declared fields, preserving every other RDF property."""

    root = _parse(original)
    by_field = {change.field: change.value for change in changes}
    unknown = sorted(set(by_field) - {"rating", "label", "keywords"})
    if unknown:
        raise ValueError(f"unsupported XMP fields: {unknown}")
    if "rating" in by_field:
        _set_scalar(root, NS_XMP, "Rating", by_field["rating"])
    if "label" in by_field:
        _set_scalar(root, NS_XMP, "Label", by_field["label"])
    if "keywords" in by_field:
        keyword_ownership = ownership.get("keywords", {})
        previously_owned = set(
            keyword_ownership.get("owned", [])
            if isinstance(keyword_ownership, dict)
            else []
        )
        preserved = [value for value in _all_keywords(root) if value not in previously_owned]
        desired = [str(value) for value in by_field["keywords"]]
        merged = list(dict.fromkeys([*preserved, *desired]))
        _set_keywords(root, merged)
    encoded = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return encoded, _values(root)


def _inside(root: Path, relative: str) -> Path:
    value = Path(relative)
    if value.is_absolute() or ".." in value.parts:
        raise ValueError(f"delivery path must be relative: {relative}")
    candidate = root / value
    if candidate.is_symlink():
        raise ValueError(f"delivery path must not be a symlink: {candidate}")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"delivery path escapes its root: {relative}")
    return candidate


def _owned_directory(root: Path, relative: Path) -> Path:
    if root.is_symlink():
        raise ValueError(f"delivery output root must not be a symlink: {root}")
    root.mkdir(parents=True, exist_ok=True)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"delivery-owned path must not be a symlink: {current}")
        current.mkdir(exist_ok=True)
        if not current.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"delivery-owned path escapes output root: {current}")
    return current


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.is_file():
            temporary.unlink()


def _ownership_for(plan: DeliveryPlan, asset_id: str) -> dict[str, Any]:
    value = plan.previous_receipt.get("field_ownership", {}).get(asset_id, {})
    return value if isinstance(value, dict) else {}


def stage_xmp(plan: DeliveryPlan, mapping: AssetMapping) -> StagedXmp:
    """Validate preimages and prepare staged/backup bytes without source mutation."""

    if plan.status != "ready" or plan.backend != "xmp":
        raise ValueError("only a ready XMP plan can be staged")
    if not plan.plan_id or plan.source_root is None or plan.output_root is None:
        raise ValueError("delivery plan lacks transaction roots or identity")
    if mapping.source_path is None or mapping.metadata_path is None:
        raise ValueError("delivery mapping lacks source or metadata path")
    source_root = Path(plan.source_root).expanduser().resolve()
    output_root = Path(plan.output_root).expanduser().resolve()
    source_path = _inside(source_root, mapping.source_path)
    target_path = _inside(source_root, mapping.metadata_path)
    if target_path != source_path.with_suffix(".xmp"):
        raise ValueError("XMP sidecar must be adjacent to its mapped source")
    if not source_path.is_file():
        raise FileNotFoundError(f"delivery source does not exist: {source_path}")
    expected = mapping.expected_before
    actual_source_sha = _sha256(source_path)
    if actual_source_sha != expected.get("source_sha256"):
        raise ValueError(f"source preimage changed for asset {mapping.asset_id}")
    target_existed = target_path.is_file()
    target_sha = _sha256(target_path) if target_existed else None
    if target_existed != bool(expected.get("metadata_exists")) or target_sha != expected.get(
        "metadata_sha256"
    ):
        raise ValueError(f"metadata preimage changed for asset {mapping.asset_id}")
    original = target_path.read_bytes() if target_existed else None
    changes = [change for change in plan.proposed_changes if change.asset_id == mapping.asset_id]
    encoded, readback = merge_xmp(original, changes, _ownership_for(plan, mapping.asset_id))

    transaction_root = _owned_directory(output_root, Path("delivery") / plan.plan_id)
    staging = _owned_directory(transaction_root, Path("staging"))
    backups = _owned_directory(transaction_root, Path("backups"))
    staged_path = staging / f"{mapping.asset_id}.xmp"
    if staged_path.is_symlink():
        raise ValueError(f"staged path must not be a symlink: {staged_path}")
    _atomic_write_bytes(staged_path, encoded)
    backup_path: Path | None = None
    if target_existed:
        assert target_sha is not None
        backup_path = backups / f"{target_sha}.xmp"
        if backup_path.is_symlink():
            raise ValueError(f"backup path must not be a symlink: {backup_path}")
        if not backup_path.exists():
            shutil.copyfile(target_path, backup_path)
        if _sha256(backup_path) != target_sha:
            raise ValueError(f"backup verification failed for asset {mapping.asset_id}")
    return StagedXmp(
        plan_id=plan.plan_id,
        asset_id=mapping.asset_id,
        source_path=source_path,
        target_path=target_path,
        staged_path=staged_path,
        backup_path=backup_path,
        source_sha256=actual_source_sha,
        target_existed=target_existed,
        target_sha256=target_sha,
        staged_sha256=_sha256(staged_path),
        readback=readback,
    )


def commit_staged_xmp(staged: StagedXmp) -> dict[str, Any]:
    """Recheck preimages, atomically replace the sidecar, and verify readback."""

    if _sha256(staged.source_path) != staged.source_sha256:
        raise ValueError(f"source preimage changed for asset {staged.asset_id}")
    exists = staged.target_path.is_file()
    current_sha = _sha256(staged.target_path) if exists else None
    if exists != staged.target_existed or current_sha != staged.target_sha256:
        raise ValueError(f"metadata preimage changed for asset {staged.asset_id}")
    if _sha256(staged.staged_path) != staged.staged_sha256:
        raise ValueError(f"staged bytes changed for asset {staged.asset_id}")
    _atomic_write_bytes(staged.target_path, staged.staged_path.read_bytes())
    readback = read_xmp(staged.target_path)
    if readback != staged.readback:
        raise ValueError(f"XMP readback mismatch for asset {staged.asset_id}")
    return readback
