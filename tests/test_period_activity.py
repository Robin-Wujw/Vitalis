from datetime import date, datetime, timedelta, timezone

import pytest

from vitalis.config import settings

from vitalis.intelligence.contracts import EnergyObservation, Provenance
from vitalis.intelligence.period_activity import build_period_activity_metrics
from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint


TARGET = date(2026, 8, 28)


def _point(metric, day, value, unit, *, source="zepp", scope="device", device="watch"):
    return SeriesPoint(
        metric=metric,
        value=value,
        unit=unit,
        day=day,
        observed_at=day,
        source=source,
        source_scope=scope,
        device_id=device,
    )


def test_period_activity_selects_one_provenance_stream_and_compares_both_periods():
    raw = RawDailyProfile(user_id="period-stream", day=TARGET)
    for offset in range(14):
        day = TARGET - timedelta(days=offset)
        value = 8_000 if offset < 7 else 6_000
        raw.series.setdefault("steps", []).append(
            _point("steps", day, value, "steps")
        )
        raw.series["steps"].append(
            _point(
                "steps", day, value + 999, "steps",
                source="zepp", scope="user_fused", device=None,
            )
        )

    metric = next(
        item for item in build_period_activity_metrics(
            raw, TARGET - timedelta(days=6), 7, TARGET - timedelta(days=13)
        ) if item.metric == "steps"
    )

    assert metric.provenance.source_scope == "user_fused"
    assert metric.provenance.device_id is None
    assert metric.available_days == 7
    assert metric.complete_days == 7
    assert metric.previous_available_days == 7
    assert metric.total == 62_993
    assert metric.change_percent == metric.total_change_percent
    assert metric.totals_are_partial is False


def test_period_activity_keeps_partial_totals_and_does_not_compare_total():
    raw = RawDailyProfile(user_id="period-partial", day=TARGET)
    raw.series["steps"] = [
        _point("steps", TARGET - timedelta(days=offset), 1_000, "steps")
        for offset in range(4)
    ]
    metrics = build_period_activity_metrics(
        raw, TARGET - timedelta(days=6), 7, TARGET - timedelta(days=13)
    )
    metric = next(item for item in metrics if item.metric == "steps")

    assert metric.available_days == 4
    assert metric.complete_days == 4
    assert metric.total == 4_000
    assert metric.totals_are_partial is True
    assert metric.total_change_percent is None
    assert metric.limitations


def test_period_activity_keeps_energy_roles_and_source_fields_separate():
    raw = RawDailyProfile(user_id="period-energy", day=TARGET)
    for offset in range(7):
        day = TARGET - timedelta(days=offset)
        raw.energy_observations.extend([
            EnergyObservation(
                metric="calories",
                value=100,
                unit="kcal",
                observed_at=day,
                provenance=Provenance(
                    source="zepp", source_scope="user_fused", device_id=None
                ),
                role="daily_total",
                source_field="daily.calories",
            ),
            EnergyObservation(
                metric="calories",
                value=40,
                unit="kcal",
                observed_at=day,
                provenance=Provenance(
                    source="zepp", source_scope="workout_summary", device_id=None
                ),
                role="workout",
                source_field="workout.calories",
            ),
        ])

    metrics = build_period_activity_metrics(
        raw, TARGET - timedelta(days=6), 7, TARGET - timedelta(days=13)
    )
    energy = [item for item in metrics if item.metric == "calories"]

    assert {(item.role, item.source_field) for item in energy} == {
        ("daily_total", "daily.calories"),
        ("workout", "workout.calories"),
    }
    assert {item.total for item in energy} == {280, 700}


@pytest.mark.parametrize("period_days", [7, 28])
def test_period_activity_sums_separate_workouts_but_not_daily_snapshots(period_days):
    raw = RawDailyProfile(user_id="period-multiple-workouts", day=TARGET)
    for role in ("workout", "unspecified"):
        for hour, value in ((1, 100), (3, 300), (5, 100)):
            raw.energy_observations.append(EnergyObservation(
                metric="calories", value=value, unit="kcal",
                observed_at=datetime.combine(TARGET, datetime.min.time()).replace(
                    hour=hour, tzinfo=timezone.utc,
                ),
                provenance=Provenance(source="zepp", source_scope="user_fused"),
                role=role, source_field=f"{role}.calories",
            ))
    metrics = build_period_activity_metrics(
        raw, TARGET - timedelta(days=period_days - 1), period_days,
    )
    by_role = {item.role: item for item in metrics}
    assert by_role["workout"].total == 500
    assert by_role["workout"].average == 500
    assert by_role["workout"].available_days == 1
    assert by_role["unspecified"].total == 100


@pytest.mark.parametrize("period_days", [7, 28])
@pytest.mark.parametrize("tz", [None, timezone.utc])
def test_period_energy_uses_local_day_at_both_window_boundaries(monkeypatch, period_days, tz):
    monkeypatch.setattr(settings, "timezone", "Asia/Shanghai")
    start = TARGET - timedelta(days=period_days - 1)
    raw = RawDailyProfile(user_id="period-local-boundaries", day=TARGET)
    for utc_day, value in ((start - timedelta(days=1), 100), (TARGET, 300)):
        raw.energy_observations.append(EnergyObservation(
            metric="calories", value=value, unit="kcal",
            observed_at=datetime.combine(utc_day, datetime.min.time()).replace(
                hour=17, tzinfo=tz,
            ),
            provenance=Provenance(source="zepp", source_scope="workout_summary"),
            role="workout", source_field="workout.calories",
        ))
    metric, = build_period_activity_metrics(raw, start, period_days)
    assert metric.total == 100
    assert metric.available_days == 1
    assert metric.previous_available_days == 0
    assert metric.previous_total is None
