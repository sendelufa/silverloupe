import json
from collections import Counter
from pathlib import Path


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
