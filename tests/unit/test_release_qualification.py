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


def _artifact_identity(artifact: Path) -> dict[str, object]:
    return {
        "path": str(artifact.resolve()),
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "release_version": "0.1.0",
    }


def _write_trial_evidence(
    evidence: Path,
    artifact: Path,
    *,
    status: str = "passed",
    artifact_identity: dict[str, object] | None = None,
    release_gates: dict[str, object] | None = None,
) -> None:
    result_path = evidence / "trials/case/codex/b3/1/trial-result.json"
    result_path.parent.mkdir(parents=True)
    identity = artifact_identity or _artifact_identity(artifact)
    result_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "case_id": "case",
                "host": "codex",
                "variant": "b3",
                "output_dir": str(result_path.parent),
                "status": status,
                "wall_time_seconds": 1.0,
                "tokens": {},
                "cost": {},
                "versions": {},
                "errors": [] if status == "passed" else [status],
                "metadata": {"artifact": identity},
            }
        ),
        encoding="utf-8",
    )
    trial_summary = {
        "trials": 1,
        "statuses": {
            "passed": int(status == "passed"),
            "partial": int(status == "partial"),
            "failed": int(status == "failed"),
        },
        "runtime_reliability": 1.0 if status == "passed" else 0.0,
    }
    if release_gates is not None:
        trial_summary["release_gates"] = release_gates
    (evidence / "summary.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "suite": "release",
                "artifact": identity,
                "trial_summary": trial_summary,
                "results": [
                    result_path.relative_to(evidence).as_posix()
                ],
            }
        ),
        encoding="utf-8",
    )


def _rewrite_with_coherent_additions(
    source: Path, destination: Path, additions: dict[str, bytes]
) -> Path:
    destination.parent.mkdir(parents=True)
    with tarfile.open(source, "r:gz") as archive:
        members = {
            member.name: archive.extractfile(member).read()
            for member in archive.getmembers()
            if member.isfile() and archive.extractfile(member) is not None
        }
    root = "silverloupe-0.1.0"
    checksums_name = f"{root}/checksums.json"
    manifest_name = f"{root}/manifest.json"
    checksums = json.loads(members[checksums_name])
    manifest = json.loads(members[manifest_name])
    for relative, data in additions.items():
        members[f"{root}/{relative}"] = data
        checksums[relative] = hashlib.sha256(data).hexdigest()
        manifest["ownership"].append(relative)
    members[checksums_name] = (json.dumps(checksums, sort_keys=True) + "\n").encode()
    members[manifest_name] = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    with tarfile.open(destination, "w:gz") as changed:
        for name, data in sorted(members.items()):
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
    _write_trial_evidence(evidence, artifact, status="failed")

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


def test_qualify_release_consumes_runner_nested_trial_summary(tmp_path: Path) -> None:
    artifact = build_release(ROOT, tmp_path / "dist", "0.1.0")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    _write_trial_evidence(evidence, artifact)

    result = qualify_release(artifact, source_root=ROOT, evidence_root=evidence)

    assert result["decision"] == "recommendation-only"
    assert result["trial_evidence"]["status"] == "passed"
    assert "release trial summary is invalid" not in result["blockers"]


def test_qualify_release_blocks_evidence_for_different_artifact(tmp_path: Path) -> None:
    artifact = build_release(ROOT, tmp_path / "dist", "0.1.0")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    _write_trial_evidence(
        evidence,
        artifact,
        artifact_identity={
            "path": "/stale/silverloupe-9.9.9.tar.gz",
            "sha256": "0" * 64,
            "release_version": "9.9.9",
        },
    )

    result = qualify_release(artifact, source_root=ROOT, evidence_root=evidence)

    assert result["decision"] == "blocked"
    assert result["trial_evidence"]["status"] == "invalid"
    assert "release trial evidence targets a different artifact" in result["blockers"]


def test_qualify_release_rejects_coherent_non_allowlisted_payload(tmp_path: Path) -> None:
    artifact = build_release(ROOT, tmp_path / "dist", "0.1.0")
    poisoned = _rewrite_with_coherent_additions(
        artifact,
        tmp_path / "poisoned/silverloupe-0.1.0.tar.gz",
        {
            "skills/silverloupe-eval/SKILL.md": b"dev only\n",
            "traces/private.json": b"{}\n",
        },
    )

    result = qualify_release(poisoned, source_root=ROOT)

    assert result["decision"] == "blocked"
    assert result["archive"]["unexpected_members"] == [
        "skills/silverloupe-eval/SKILL.md",
        "traces/private.json",
    ]


def test_missing_dataset_manifest_cannot_enable_auto_reject(tmp_path: Path) -> None:
    artifact = build_release(ROOT, tmp_path / "dist", "0.1.0")
    source = tmp_path / "source"
    (source / "evals").mkdir(parents=True)
    (source / "evals/compatibility-matrix.json").write_text(
        json.dumps({"combinations": [{"status": "verified"}]}),
        encoding="utf-8",
    )
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    _write_trial_evidence(
        evidence,
        artifact,
        release_gates={
            "false_reject_upper_bound": 0.001,
            "protected_case_failures": 0,
            "runtime_reliability": 1.0,
        },
    )

    result = qualify_release(artifact, source_root=source, evidence_root=evidence)

    assert result["decision"] == "blocked"
    assert result["auto_reject_enabled"] is False
    assert "dataset manifest is unavailable or invalid" in result["blockers"]


def test_partial_trials_cannot_enable_auto_reject(tmp_path: Path) -> None:
    artifact = build_release(ROOT, tmp_path / "dist", "0.1.0")
    source = tmp_path / "source"
    (source / "evals").mkdir(parents=True)
    (source / "evals/dataset-manifest.json").write_text(
        json.dumps(
            {
                "datasets": [
                    {"dataset_id": "sealed-1", "availability": "available"}
                ]
            }
        ),
        encoding="utf-8",
    )
    (source / "evals/compatibility-matrix.json").write_text(
        json.dumps({"combinations": [{"status": "verified"}]}),
        encoding="utf-8",
    )
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    _write_trial_evidence(
        evidence,
        artifact,
        status="partial",
        release_gates={
            "false_reject_upper_bound": 0.001,
            "protected_case_failures": 0,
            "runtime_reliability": 1.0,
        },
    )

    result = qualify_release(artifact, source_root=source, evidence_root=evidence)

    assert result["decision"] == "recommendation-only"
    assert result["auto_reject_enabled"] is False
