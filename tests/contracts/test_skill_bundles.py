import json
import re
import shutil
from collections import Counter
from pathlib import Path

import pytest

from tools.build_collection import build_collection


ROOT = Path(__file__).parents[2]
ROUTING_CASES = ROOT / "evals/cases/routing.jsonl"
B2_BASELINE = ROOT / "evals/baselines/issue-6-b2.jsonl"


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_routing_corpus_has_20_positive_20_negative_20_boundary_cases() -> None:
    cases = _read_jsonl(ROUTING_CASES)

    assert len(cases) == 60
    assert Counter(case["category"] for case in cases) == {
        "positive": 20,
        "negative": 20,
        "boundary": 20,
    }
    assert all(case["schema_version"] == 1 for case in cases)
    assert all(
        set(case)
        == {
            "schema_version",
            "case_id",
            "language",
            "category",
            "prompt",
            "expected_skill",
            "required_disclosures",
        }
        for case in cases
    )
    assert all(case["expected_skill"] is not None for case in cases if case["category"] == "positive")
    assert all(case["expected_skill"] is None for case in cases if case["category"] == "negative")


def test_routing_corpus_is_bilingual_and_has_unique_ids() -> None:
    cases = _read_jsonl(ROUTING_CASES)
    ids = [case["case_id"] for case in cases]

    assert {case["language"] for case in cases} == {"ru", "en"}
    assert len(ids) == len(set(ids))
    assert all(case["prompt"].strip() for case in cases)


def test_b2_baseline_records_observed_failures_without_private_reasoning() -> None:
    records = _read_jsonl(B2_BASELINE)

    assert len(records) == 6
    assert all(record["schema_version"] == 1 for record in records)
    assert all(record["host"] == "codex" for record in records)
    assert all(record["cli_version"] for record in records)
    assert all(record["prompt"] and record["final_response"] for record in records)
    assert all(isinstance(record["observable_actions"], list) for record in records)
    assert all(record["trace_visibility"] in {"available", "unavailable"} for record in records)
    assert any(record["failure_categories"] for record in records)
    forbidden = {"reasoning", "chain_of_thought", "private_reasoning", "analysis"}
    assert all(not forbidden.intersection(record) for record in records)


PRODUCTION_SKILLS = {
    "silverloupe",
    "silverloupe-prepare",
    "silverloupe-triage",
    "silverloupe-compare",
    "silverloupe-curate",
    "silverloupe-deliver",
}


def _frontmatter(document: str) -> dict[str, str]:
    assert document.startswith("---\n")
    block = document.split("---\n", 2)[1]
    values: dict[str, str] = {}
    active: str | None = None
    for line in block.splitlines():
        if line.startswith("  ") and active:
            values[active] += " " + line.strip()
            continue
        key, separator, value = line.partition(":")
        assert separator
        active = key.strip()
        values[active] = value.strip().lstrip(">").strip()
    return {key: value.strip() for key, value in values.items()}


def _links(path: Path) -> list[str]:
    return re.findall(r"\[[^\]]+\]\(([^)]+)\)", path.read_text(encoding="utf-8"))


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_source_collection_has_exact_valid_production_skills() -> None:
    skill_root = ROOT / "skills"
    assert {path.name for path in skill_root.iterdir() if path.is_dir()} == PRODUCTION_SKILLS
    descriptions: set[str] = set()
    for name in PRODUCTION_SKILLS:
        path = skill_root / name / "SKILL.md"
        document = path.read_text(encoding="utf-8")
        frontmatter = _frontmatter(document)
        assert frontmatter["name"] == name
        assert frontmatter["description"].startswith("Use when")
        assert frontmatter["description"] not in descriptions
        descriptions.add(frontmatter["description"])
        assert "silverloupe==0.1.0" in document
        assert 80 <= len(document.splitlines()) <= 250
        assert "../../resources/contracts.md" in document


def test_source_skills_have_closed_conditional_references_and_no_dev_leakage() -> None:
    forbidden = ("evals/", "tests/", "docs/superpowers", "/Users/", "/home/")
    for name in PRODUCTION_SKILLS:
        path = ROOT / "skills" / name / "SKILL.md"
        document = path.read_text(encoding="utf-8")
        assert not any(value in document for value in forbidden)
        for link in _links(path):
            assert not Path(link).is_absolute()
            assert (path.parent / link).resolve().is_relative_to(ROOT.resolve())
            assert (path.parent / link).resolve().is_file()
        assert "If " in document or "When " in document
    curate = (ROOT / "skills/silverloupe-curate/SKILL.md").read_text(encoding="utf-8")
    assert all(f"../../resources/genres/{genre}.md" in curate for genre in ("wedding", "architecture", "landscape"))
    triage = (ROOT / "skills/silverloupe-triage/SKILL.md").read_text(encoding="utf-8")
    assert "../../resources/artistic-intent.md" in triage


def test_eval_skill_is_dev_only_and_does_not_trigger_for_ordinary_selection() -> None:
    path = ROOT / "dev/skills/silverloupe-eval/SKILL.md"
    document = path.read_text(encoding="utf-8")
    frontmatter = _frontmatter(document)
    assert frontmatter["name"] == "silverloupe-eval"
    assert "benchmark" in frontmatter["description"].lower()
    assert "ordinary photo selection" in frontmatter["description"].lower()
    assert 80 <= len(document.splitlines()) <= 250


def test_runtime_and_dev_bundles_are_closed_and_separate(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    dev = tmp_path / "dev-toolkit"

    runtime_manifest = build_collection(ROOT, runtime, kind="runtime")
    dev_manifest = build_collection(ROOT, dev, kind="dev-toolkit")

    assert {path.name for path in (runtime / "skills").iterdir()} == PRODUCTION_SKILLS
    assert not any("silverloupe-eval" in path.as_posix() for path in runtime.rglob("*"))
    assert not any(part in {"evals", "tests", "docs"} for path in runtime.rglob("*") for part in path.parts)
    assert {path.name for path in (dev / "skills").iterdir()} == {"silverloupe-eval"}
    assert (dev / "skills/silverloupe-eval/references/routing.jsonl").is_file()
    assert (dev / "skills/silverloupe-eval/references/issue-6-b2.jsonl").is_file()
    assert runtime_manifest["kind"] == "runtime"
    assert dev_manifest["kind"] == "dev-toolkit"
    for root in (runtime, dev):
        for markdown in root.rglob("*.md"):
            for link in _links(markdown):
                target = (markdown.parent / link).resolve()
                assert target.is_relative_to(root.resolve())
                assert target.is_file()


def test_runtime_bundle_preserves_canonical_reference_bytes_and_is_deterministic(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    build_collection(ROOT, first, kind="runtime")
    build_collection(ROOT, second, kind="runtime")

    assert _tree_bytes(first) == _tree_bytes(second)
    assert (
        first / "skills/silverloupe/references/contracts.md"
    ).read_bytes() == (ROOT / "resources/contracts.md").read_bytes()


def test_bundle_builder_rejects_nonempty_destination_traversal_and_symlink_sources(
    tmp_path: Path,
) -> None:
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        build_collection(ROOT, nonempty, kind="runtime")

    copied = tmp_path / "source"
    shutil.copytree(ROOT / "skills", copied / "skills")
    shutil.copytree(ROOT / "resources", copied / "resources")
    (copied / "skills/silverloupe/SKILL.md").unlink()
    (copied / "skills/silverloupe/SKILL.md").symlink_to(ROOT / "skills/silverloupe/SKILL.md")
    with pytest.raises(ValueError, match="symlink"):
        build_collection(copied, tmp_path / "symlink-output", kind="runtime")

    malicious = tmp_path / "malicious"
    shutil.copytree(ROOT / "skills", malicious / "skills")
    shutil.copytree(ROOT / "resources", malicious / "resources")
    skill = malicious / "skills/silverloupe/SKILL.md"
    skill.write_text(skill.read_text() + "\n[escape](../../../outside.md)\n", encoding="utf-8")
    with pytest.raises(ValueError, match="reference"):
        build_collection(malicious, tmp_path / "traversal-output", kind="runtime")
