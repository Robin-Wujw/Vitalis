"""Personal next-day response model and its honesty check.

For each day the model relates training load, sleep and the user's behaviour
tags to the next night's HRV, resting heart rate and the next day's feeling.
It is a ridge regression on the user's own days, scored by rolling-origin
prediction (train on earlier days only) against the naive "tomorrow equals
today" forecast.  It turns on only with enough feedback days and is used only
for a target it predicts better than that baseline.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from math import log

MIN_FEEDBACK_DAYS = 60
MIN_TRAIN_DAYS = 28
RIDGE = 1.0
TAGS = ("alcohol", "late_dinner", "caffeine", "illness", "travel")
TARGETS = ("hrv", "resting_hr", "feeling")


@dataclass(frozen=True)
class DayRow:
    day: date
    load: float | None
    sleep_minutes: float | None
    hrv: float | None          # ln of nightly HRV for the sleep that ends on ``day``
    resting_hr: float | None
    feeling: float | None
    tags: frozenset[str]


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting for a small dense system."""
    size = len(vector)
    rows = [list(matrix[index]) + [vector[index]] for index in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda index: abs(rows[index][column]))
        rows[column], rows[pivot] = rows[pivot], rows[column]
        if abs(rows[column][column]) < 1e-12:
            continue
        for index in range(size):
            if index != column:
                factor = rows[index][column] / rows[column][column]
                rows[index] = [a - factor * b for a, b in zip(rows[index], rows[column])]
    return [rows[index][size] / rows[index][index] if abs(rows[index][index]) > 1e-12 else 0.0 for index in range(size)]


def _features(row: DayRow, target: str) -> list[float] | None:
    today = {"hrv": row.hrv, "resting_hr": row.resting_hr, "feeling": row.feeling}[target]
    if today is None or row.load is None or row.sleep_minutes is None:
        return None
    return [1.0, today, row.load / 100, row.sleep_minutes / 60] + [float(tag in row.tags) for tag in TAGS]


def _fit(samples: list[tuple[list[float], float]]) -> list[float]:
    width = len(samples[0][0])
    gram = [[0.0] * width for _ in range(width)]
    moment = [0.0] * width
    for features, outcome in samples:
        for i in range(width):
            moment[i] += features[i] * outcome
            for j in range(width):
                gram[i][j] += features[i] * features[j]
    for i in range(1, width):  # do not shrink the intercept
        gram[i][i] += RIDGE
    return _solve(gram, moment)


def evaluate(rows: list[DayRow]) -> dict:
    """Rolling-origin error of the model and of the naive forecast per target."""
    by_day = {row.day: row for row in rows}
    feedback_days = sum(1 for row in rows if row.feeling is not None or row.tags)
    output: dict = {
        "feedback_days": feedback_days,
        "required_feedback_days": MIN_FEEDBACK_DAYS,
        "enabled": feedback_days >= MIN_FEEDBACK_DAYS,
        "targets": {},
    }
    for target in TARGETS:
        pairs = []
        for row in sorted(rows, key=lambda item: item.day):
            following = by_day.get(row.day + timedelta(days=1))
            features = _features(row, target)
            outcome = None if following is None else {
                "hrv": following.hrv, "resting_hr": following.resting_hr, "feeling": following.feeling,
            }[target]
            if features is not None and outcome is not None:
                pairs.append((row.day, features, outcome))
        model_errors, naive_errors = [], []
        for index in range(MIN_TRAIN_DAYS, len(pairs)):
            coefficients = _fit([(features, outcome) for _, features, outcome in pairs[:index]])
            _, features, outcome = pairs[index]
            prediction = sum(a * b for a, b in zip(coefficients, features))
            model_errors.append(abs(prediction - outcome))
            naive_errors.append(abs(features[1] - outcome))
        if model_errors:
            model_mae = sum(model_errors) / len(model_errors)
            naive_mae = sum(naive_errors) / len(naive_errors)
            output["targets"][target] = {
                "evaluated_days": len(model_errors),
                "model_mae": round(model_mae, 4),
                "naive_mae": round(naive_mae, 4),
                "beats_naive": model_mae < naive_mae,
                "usable": output["enabled"] and model_mae < naive_mae,
            }
        else:
            output["targets"][target] = {"evaluated_days": 0, "usable": False}
    return output


def rows_from_series(
    series: dict, feedback: list[dict], start: date, end: date, *, verified_training_days: set[date],
) -> list[DayRow]:
    """Daily rows from profile series (``SeriesPoint``) and saved feedback dicts.

    A day without a training record counts as zero load only when the
    training ledger verified it; otherwise its load stays unknown.
    """

    def daily(metrics: tuple[str, ...]) -> dict[date, float]:
        for metric in metrics:
            values: dict[date, list[float]] = defaultdict(list)
            for point in series.get(metric, []):
                if start <= point.day <= end:
                    values[point.day].append(point.value)
            if values:
                return {day: sorted(items)[len(items) // 2] for day, items in values.items()}
        return {}

    hrv = {day: log(value) for day, value in daily(("sleep_hrv", "hrv_rmssd")).items() if value > 0}
    rhr = daily(("sleep_rhr", "resting_hr"))
    load = daily(("training_load",))
    sleep = daily(("sleep_duration",))
    feelings: dict[date, float] = {}
    tags: dict[date, set[str]] = defaultdict(set)
    for item in feedback:
        day = item.get("date")
        day = date.fromisoformat(str(day)[:10]) if day else None
        if day is None or not start <= day <= end:
            continue
        if item.get("feeling_score") is not None:
            feelings[day] = float(item["feeling_score"])
        tags[day].update(tag for tag in item.get("behavior_tags") or [] if tag in TAGS)
    days = sorted(set(hrv) | set(rhr) | set(load) | set(sleep) | set(feelings) | set(tags))
    return [
        DayRow(
            day=day, load=load.get(day, 0.0 if day in verified_training_days else None),
            sleep_minutes=sleep.get(day), hrv=hrv.get(day), resting_hr=rhr.get(day),
            feeling=feelings.get(day), tags=frozenset(tags.get(day, ())),
        )
        for day in days
    ]
