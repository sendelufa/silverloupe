from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

from evals.release import qualify_release
from tools.build_collection import build_release


ROOT = Path(__file__).parents[2]


def _rewrite_archive(
    source: Path,
    destination: Path,
    *,
    replacements: dict[str, bytes] | None = None,
    additions: dict[str, bytes] | None = None,
) -> Path:
    replacements = replacements or {}
    additions = additions or {}
    with tarfile.open(source, "r:gz") as original, tarfile.open(destination, "w:gz") as changed:
        for member in original.getmembers():
            extracted = original.extractfile(member)
            data = extracted.read() if extracted is not None else b""
            data = replacements.get(member.name, data)
            copy = tarfile.TarInfo(member.name)
            copy.size = len(data)
            copy.mode = member.mode
            changed.addfile(copy, io.BytesIO(data))
        for name, data in additions.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o644
            changed.addfile(member, io.BytesIO(data))
    return destination


def test_qualify_release_records_exact_artifact_and_current_fail_closed_gates(
    tmp_path: Path,
) -> None:
    artifact = build_release(ROOT, tmp_path / "dist", "0.1.0")

    result = qualify_release(artifact, source_root=ROOT)

    assert result["artifact"] == {
        "path": str(artifact.resolve()),
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "size_bytes": artifact.stat().st_size,
        "release_version": "0.1.0",
        "core_version": "0.1.0",
        "skills_version": "0.1.0",
    }
    assert result["archive"]["valid"] is True
    assert result["archive"]["forbidden_members"] == []
    assert result["archive"]["checksum_mismatches"] == []
    assert result["datasets"] == {
        "available": 0,
        "unavailable": 6,
        "unavailable_ids": [
            "pilot-architecture-01",
            "pilot-architecture-02",
            "pilot-landscape-01",
            "pilot-landscape-02",
            "pilot-wedding-01",
            "pilot-wedding-02",
        ],
    }
    assert result["editor_compatibility"]["statuses"] == {"not-tested": 10}
    assert result["decision"] == "recommendation-only"
    assert result["auto_reject_enabled"] is False
    assert "real photo datasets are unavailable" in result["blockers"]
    assert "editor round-trip combinations remain unverified" in result["blockers"]


def test_qualify_release_blocks_archive_with_forbidden_development_member(
    tmp_path: Path,
) -> None:
    artifact = build_release(ROOT, tmp_path / "dist", "0.1.0")
    poisoned = _rewrite_archive(
        artifact,
        tmp_path / "silverloupe-0.1.0-poisoned.tar.gz",
        additions={"silverloupe-0.1.0/evals/gold.json": b"{}\n"},
    )

    result = qualify_release(poisoned, source_root=ROOT)

    assert result["decision"] == "blocked"
    assert result["archive"]["valid"] is False
    assert result["archive"]["forbidden_members"] == ["evals/gold.json"]


def test_qualify_release_blocks_checksum_mismatch(tmp_path: Path) -> None:
    artifact = build_release(ROOT, tmp_path / "dist", "0.1.0")
    poisoned = _rewrite_archive(
        artifact,
        tmp_path / "silverloupe-0.1.0-poisoned.tar.gz",
        replacements={"silverloupe-0.1.0/runtime/src/silverloupe/__init__.py": b"tampered\n"},
    )

    result = qualify_release(poisoned, source_root=ROOT)

    assert result["decision"] == "blocked"
    assert result["archive"]["checksum_mismatches"] == [
        "runtime/src/silverloupe/__init__.py"
    ]


def test_qualify_release_blocks_filename_and_manifest_version_mismatch(
    tmp_path: Path,
) -> None:
    artifact = build_release(ROOT, tmp_path / "dist", "0.1.0")
    renamed = tmp_path / "silverloupe-9.9.9.tar.gz"
    renamed.write_bytes(artifact.read_bytes())

    result = qualify_release(renamed, source_root=ROOT)

    assert result["decision"] == "blocked"
    assert any("filename version" in error for error in result["archive"]["errors"])


def test_qualify_release_blocks_failed_trial_evidence(tmp_path: Path) -> None:
    artifact = build_release(ROOT, tmp_path / "dist", "0.1.0")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "summary.json").write_text(
        json.dumps(
            {
                "trials": 3,
                "statuses": {"passed": 2, "partial": 0, "failed": 1},
            }
        ),
        encoding="utf-8",
    )

    result = qualify_release(artifact, source_root=ROOT, evidence_root=evidence)

    assert result["decision"] == "blocked"
    assert result["trial_evidence"]["status"] == "failed"
    assert "one or more release trials failed" in result["blockers"]


def test_qualify_release_marks_missing_trial_summary_unavailable(tmp_path: Path) -> None:
    artifact = build_release(ROOT, tmp_path / "dist", "0.1.0")

    result = qualify_release(
        artifact,
        source_root=ROOT,
        evidence_root=tmp_path / "missing-evidence",
    )

    assert result["decision"] == "recommendation-only"
    assert result["trial_evidence"] == {
        "status": "unavailable",
        "reason": "summary.json was not found",
    }
