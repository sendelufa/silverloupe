from __future__ import annotations

import pytest

from evals.contracts import TrialResult
from evals.metrics import (
    ranking_metrics,
    rejection_metrics,
    routing_metrics,
    safe_ratio,
    summarize_trials,
    zero_error_upper_bound,
)


def _trial(
    case_id: str,
    *,
    host: str,
    variant: str,
    status: str,
    wall_time_seconds: float,
    tokens: dict[str, int] | None = None,
    cost: dict[str, float] | None = None,
) -> TrialResult:
    return TrialResult(
        case_id=case_id,
        host=host,
        variant=variant,
        output_dir=f"/runs/{case_id}",
        status=status,
        wall_time_seconds=wall_time_seconds,
        tokens=tokens or {},
        cost=cost or {},
    )


def test_safe_ratio_marks_empty_denominator_unavailable() -> None:
    assert safe_ratio(0, 0) is None
    assert safe_ratio(1, 4) == 0.25


def test_rejection_metrics_use_gold_counts_without_inflating_empty_precision() -> None:
    result = rejection_metrics(
        gold_good={f"good-{index}" for index in range(200)},
        gold_unusable={f"bad-{index}" for index in range(50)},
        auto_rejected={"good-0", *{f"bad-{index}" for index in range(49)}},
    )

    assert result == {
        "counts": {
            "all_gold_good": 200,
            "all_gold_unusable": 50,
            "all_auto_rejected": 50,
            "good_auto_rejected": 1,
            "true_unusable_auto_rejected": 49,
        },
        "good_frame_retention": 0.995,
        "false_reject_risk": 0.005,
        "reject_precision": 0.98,
        "reject_recall": 0.98,
    }

    no_rejects = rejection_metrics(
        gold_good={"good"},
        gold_unusable={"bad"},
        auto_rejected=set(),
    )
    assert no_rejects["reject_precision"] is None
    assert no_rejects["reject_recall"] == 0.0


def test_ranking_metrics_compute_ndcg_at_k_from_literal_grades() -> None:
    result = ranking_metrics(
        grades={"a": 3, "b": 2, "c": 0},
        ranked_ids=["b", "a", "c"],
        k=2,
    )

    assert result["evaluated_k"] == 2
    assert result["ndcg_at_k"] == pytest.approx(0.8339912323981488)
    assert result["unknown_ranked_ids"] == []


def test_ranking_metrics_report_unavailable_when_no_relevant_gold_exists() -> None:
    result = ranking_metrics(grades={"a": 0}, ranked_ids=["a"], k=1)

    assert result["ndcg_at_k"] is None


def test_routing_metrics_count_activation_precision_recall_and_exact_route() -> None:
    result = routing_metrics(
        expected=["silverloupe", None, "silverloupe-prepare"],
        predicted=["silverloupe", "silverloupe", None],
    )

    assert result == {
        "counts": {"true_positive": 1, "false_positive": 1, "false_negative": 1},
        "activation_precision": 0.5,
        "activation_recall": 0.5,
        "exact_route_accuracy": pytest.approx(1 / 3),
    }


def test_routing_metrics_leave_precision_and_recall_unavailable_without_examples() -> None:
    result = routing_metrics(expected=[], predicted=[])

    assert result["activation_precision"] is None
    assert result["activation_recall"] is None
    assert result["exact_route_accuracy"] is None


def test_zero_error_bound_uses_independent_shoot_count() -> None:
    assert zero_error_upper_bound(30) == pytest.approx(0.09503385285530419)
    assert zero_error_upper_bound(0) is None


def test_summarize_trials_reports_status_latency_usage_and_host_variant_breakdown() -> None:
    result = summarize_trials(
        [
            _trial(
                "one",
                host="codex",
                variant="b3",
                status="passed",
                wall_time_seconds=1.0,
                tokens={"input": 10, "output": 2},
                cost={"usd": 0.1},
            ),
            _trial(
                "two",
                host="codex",
                variant="b3",
                status="partial",
                wall_time_seconds=2.0,
                tokens={"input": 20},
                cost={"usd": 0.2},
            ),
            _trial(
                "three",
                host="claude",
                variant="b2",
                status="failed",
                wall_time_seconds=3.0,
            ),
            _trial(
                "four",
                host="claude",
                variant="b3",
                status="passed",
                wall_time_seconds=4.0,
            ),
        ]
    )

    assert result["trials"] == 4
    assert result["statuses"] == {"passed": 2, "partial": 1, "failed": 1}
    assert result["runtime_reliability"] == 0.5
    assert result["latency_seconds"] == {"p50": 2.0, "p95": 4.0}
    assert result["tokens"] == {"input": 30, "output": 2}
    assert result["cost"] == {"usd": pytest.approx(0.3)}
    assert result["by_host_variant"] == {
        "claude/b2": {"trials": 1, "passed": 0, "partial": 0, "failed": 1},
        "claude/b3": {"trials": 1, "passed": 1, "partial": 0, "failed": 0},
        "codex/b3": {"trials": 2, "passed": 1, "partial": 1, "failed": 0},
    }


def test_summarize_trials_marks_empty_latency_and_reliability_unavailable() -> None:
    result = summarize_trials([])

    assert result["trials"] == 0
    assert result["runtime_reliability"] is None
    assert result["latency_seconds"] == {"p50": None, "p95": None}
