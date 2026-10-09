"""Small, deterministic association statistics implemented with the standard library.

The module deliberately exposes the inferential method used by the association
engine.  It is descriptive longitudinal analysis: a permutation p-value and a
Benjamini-Hochberg q-value do not turn a personal association into a causal
claim.
"""

from __future__ import annotations

from datetime import date
from hashlib import sha256
from math import isfinite
from random import Random
from typing import Sequence


DEFAULT_PERMUTATIONS = 4_095
DEFAULT_BLOCK_DAYS = 7


def finite_sequence(values: Sequence[float]) -> bool:
    """Return true only when every value is a finite real number."""
    return all(isinstance(value, (int, float)) and isfinite(float(value)) for value in values)


def average_ranks(values: Sequence[float]) -> list[float]:
    """Return one-based average ranks, retaining deterministic tie handling."""
    indexed = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(indexed):
        end = cursor + 1
        while end < len(indexed) and indexed[end][1] == indexed[cursor][1]:
            end += 1
        rank = ((cursor + 1) + end) / 2
        for position in range(cursor, end):
            ranks[indexed[position][0]] = rank
        cursor = end
    return ranks


def spearman_coefficient(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Compute tie-aware Spearman rho, abstaining on invalid or constant data."""
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    if not finite_sequence(xs) or not finite_sequence(ys):
        return None
    ranked_x = average_ranks(xs)
    ranked_y = average_ranks(ys)
    mean_x = sum(ranked_x) / len(ranked_x)
    mean_y = sum(ranked_y) / len(ranked_y)
    numerator = sum(
        (x - mean_x) * (y - mean_y) for x, y in zip(ranked_x, ranked_y)
    )
    denominator_x = sum((x - mean_x) ** 2 for x in ranked_x)
    denominator_y = sum((y - mean_y) ** 2 for y in ranked_y)
    denominator = (denominator_x * denominator_y) ** 0.5
    return numerator / denominator if denominator else None


def stable_seed(*parts: object) -> int:
    """Derive a process-independent non-negative seed from identity parts."""
    payload = "|".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(sha256(payload).digest()[:8], "big", signed=False)


def block_permutation_p_value(
    xs: Sequence[float],
    ys: Sequence[float],
    days: Sequence[object],
    *,
    seed: int,
    block_days: int = DEFAULT_BLOCK_DAYS,
    permutations: int = DEFAULT_PERMUTATIONS,
) -> float | None:
    """Estimate a two-sided Spearman p-value with calendar-week block shuffles.

    Dates must be unique and increasing. Outcome ranks within each seven-day
    calendar bucket (anchored at the first paired date) stay ordered; complete
    buckets are shuffled, including their observed missingness and ties. No
    missing day is inserted. At least six occupied buckets are required.
    This approximate test assumes exchangeable blocks and does not eliminate
    shared long-term trends, seasonality or unmeasured confounders. The local
    PRNG and the (extreme + 1)/(permutations + 1) correction are deterministic.
    """
    if len(xs) != len(ys) or len(xs) != len(days):
        return None
    if len(xs) < 3 or block_days < 1 or permutations < 1:
        return None
    if not finite_sequence(xs) or not finite_sequence(ys):
        return None
    if not all(type(day) is date for day in days):
        return None
    if any(left >= right for left, right in zip(days, days[1:])):
        return None
    observed = spearman_coefficient(xs, ys)
    if observed is None:
        return None
    ranked_x = average_ranks(xs)
    ranked_y = average_ranks(ys)
    center = (len(xs) + 1) / 2
    centered_x = [value - center for value in ranked_x]
    centered_y = [value - center for value in ranked_y]
    threshold = abs(sum(x * y for x, y in zip(centered_x, centered_y))) - 1e-9
    by_bucket: dict[int, list[float]] = {}
    for day, value in zip(days, centered_y):
        bucket = (day - days[0]).days // block_days
        by_bucket.setdefault(bucket, []).append(value)
    blocks = list(by_bucket.values())
    if len(blocks) < 6:
        return None
    rng = Random(seed)
    extreme = 0
    order = list(range(len(blocks)))
    for _ in range(permutations):
        rng.shuffle(order)
        permuted = [value for index in order for value in blocks[index]]
        statistic = abs(sum(x * y for x, y in zip(centered_x, permuted)))
        if statistic >= threshold:
            extreme += 1
    return (extreme + 1) / (permutations + 1)


def benjamini_hochberg(p_values: Sequence[float | None]) -> list[float | None]:
    """Adjust the entire registered family; untested entries count with p=1.

    Untested entries retain q=None. Their presence still increases the family
    denominator, so low coverage cannot make the remaining findings appear
    more significant simply by removing registered comparisons.
    """
    valid = [
        (index, float(value))
        for index, value in enumerate(p_values)
        if value is not None and isfinite(float(value)) and 0 <= float(value) <= 1
    ]
    output: list[float | None] = [None] * len(p_values)
    if not valid:
        return output
    ordered = sorted(valid, key=lambda item: (item[1], item[0]))
    count = len(p_values)
    running = 1.0
    for rank in range(len(ordered), 0, -1):
        index, p_value = ordered[rank - 1]
        running = min(running, p_value * count / rank)
        output[index] = min(max(running, 0.0), 1.0)
    return output
