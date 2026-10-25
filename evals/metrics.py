"""Pure, deterministic metrics for Silverloupe release evaluation."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Collection, Sequence

from evals.contracts import TrialResult


def safe_ratio(numerator: int | float, denominator: int | float) -> float | None:
    """Return a ratio, or ``None`` when the metric has no denominator."""

    if denominator == 0:
        return None
    return numerator / denominator


def rejection_metrics(
    *,
    gold_good: Collection[str],
    gold_unusable: Collection[str],
    auto_rejected: Collection[str],
) -> dict[str, object]:
    """Compute fail-safe rejection counts and rates from item identifiers."""

    good = set(gold_good)
    unusable = set(gold_unusable)
    rejected = set(auto_rejected)
    good_rejected = len(good & rejected)
    unusable_rejected = len(unusable & rejected)
    rejected_count = len(rejected)
    false_reject_risk = safe_ratio(good_rejected, len(good))
    return {
        "counts": {
            "all_gold_good": len(good),
            "all_gold_unusable": len(unusable),
            "all_auto_rejected": rejected_count,
            "good_auto_rejected": good_rejected,
            "true_unusable_auto_rejected": unusable_rejected,
        },
        "good_frame_retention": (
            None if false_reject_risk is None else 1.0 - false_reject_risk
        ),
        "false_reject_risk": false_reject_risk,
        "reject_precision": safe_ratio(unusable_rejected, rejected_count),
        "reject_recall": safe_ratio(unusable_rejected, len(unusable)),
    }


def ranking_metrics(
    *, grades: dict[str, int], ranked_ids: Sequence[str], k: int
) -> dict[str, object]:
    """Compute nDCG@K with exponential gain and report unknown candidates."""

    if k < 0:
        raise ValueError("k must be non-negative")
    evaluated = list(ranked_ids[:k])
    unknown = [item_id for item_id in evaluated if item_id not in grades]

    def dcg(values: Sequence[int]) -> float:
        return sum(
            (2**grade - 1) / math.log2(index + 2)
            for index, grade in enumerate(values)
        )

    actual = dcg([grades.get(item_id, 0) for item_id in evaluated])
    ideal = dcg(sorted(grades.values(), reverse=True)[:k])
    return {
        "evaluated_k": len(evaluated),
        "ndcg_at_k": None if ideal == 0 else actual / ideal,
        "unknown_ranked_ids": unknown,
    }


def routing_metrics(
    *, expected: Sequence[str | None], predicted: Sequence[str | None]
) -> dict[str, object]:
    """Measure activation and exact-route behavior without inventing empty scores."""

    if len(expected) != len(predicted):
        raise ValueError("expected and predicted must have the same length")
    true_positive = sum(
        expected_skill is not None and expected_skill == predicted_skill
        for expected_skill, predicted_skill in zip(expected, predicted, strict=True)
    )
    false_positive = sum(
        predicted_skill is not None and predicted_skill != expected_skill
        for expected_skill, predicted_skill in zip(expected, predicted, strict=True)
    )
    false_negative = sum(
        expected_skill is not None and predicted_skill != expected_skill
        for expected_skill, predicted_skill in zip(expected, predicted, strict=True)
    )
    exact = sum(
        expected_skill == predicted_skill
        for expected_skill, predicted_skill in zip(expected, predicted, strict=True)
    )
    return {
        "counts": {
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
        },
        "activation_precision": safe_ratio(
            true_positive, true_positive + false_positive
        ),
        "activation_recall": safe_ratio(
            true_positive, true_positive + false_negative
        ),
        "exact_route_accuracy": safe_ratio(exact, len(expected)),
    }


def zero_error_upper_bound(
    independent_observations: int, *, confidence: float = 0.95
) -> float | None:
    """Return the one-sided exact binomial bound when zero errors were observed."""

    if independent_observations < 0:
        raise ValueError("independent_observations must be non-negative")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be between zero and one")
    if independent_observations == 0:
        return None
    return 1.0 - (1.0 - confidence) ** (1.0 / independent_observations)


def _nearest_rank(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def summarize_trials(results: list[TrialResult]) -> dict[str, object]:
    """Summarize immutable trial records without upgrading partial evidence."""

    statuses = Counter(result.status for result in results)
    usage: dict[str, int] = defaultdict(int)
    cost: dict[str, float] = defaultdict(float)
    breakdown: dict[str, Counter[str]] = defaultdict(Counter)
    for result in results:
        for name, value in result.tokens.items():
            usage[name] += value
        for name, value in result.cost.items():
            cost[name] += value
        breakdown[f"{result.host}/{result.variant}"][result.status] += 1

    by_host_variant: dict[str, dict[str, int]] = {}
    for key in sorted(breakdown):
        counts = breakdown[key]
        by_host_variant[key] = {
            "trials": sum(counts.values()),
            "passed": counts["passed"],
            "partial": counts["partial"],
            "failed": counts["failed"],
        }

    wall_times = [result.wall_time_seconds for result in results]
    return {
        "trials": len(results),
        "statuses": {
            "passed": statuses["passed"],
            "partial": statuses["partial"],
            "failed": statuses["failed"],
        },
        "runtime_reliability": safe_ratio(statuses["passed"], len(results)),
        "latency_seconds": {
            "p50": _nearest_rank(wall_times, 0.50),
            "p95": _nearest_rank(wall_times, 0.95),
        },
        "tokens": dict(sorted(usage.items())),
        "cost": dict(sorted(cost.items())),
        "by_host_variant": by_host_variant,
    }
