from datetime import date, datetime, timedelta, timezone

from vitalis.application.analysis import (
    AnalysisDataset,
    AnalysisPolicy,
    AnalysisRequest,
    analyze,
)
from vitalis.intelligence.contracts import (
    DataQuality,
    QualityStatus,
    TrainingPreferences,
)
from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint


TARGET = date(2026, 8, 28)
AS_OF = datetime(2026, 8, 29, 3, 0, tzinfo=timezone.utc)


def _points(metric, values, unit):
    return [
        SeriesPoint(
            metric=metric,
            value=value,
            unit=unit,
            day=TARGET - timedelta(days=offset),
            observed_at=TARGET - timedelta(days=offset),
            source="zepp",
            source_scope="normalized_daily_record",
        )
        for offset, value in enumerate(values)
    ]


def _dataset():
    raw = RawDailyProfile(
        user_id="deterministic-user",
        day=TARGET,
        as_of=AS_OF,
        timezone_name="Asia/Shanghai",
    )
    raw.training_preferences = TrainingPreferences(user_id=raw.user_id)
    raw.data_quality = DataQuality(
        status=QualityStatus.INSUFFICIENT,
        status_label="数据不足",
    )
    raw.series = {
        "sleep_duration": _points("sleep_duration", [360] + [450] * 27, "min"),
        "hrv_rmssd": _points("hrv_rmssd", [40] + [50 + index % 3 for index in range(1, 28)], "ms"),
        "resting_hr": _points("resting_hr", [64] + [56 + index % 2 for index in range(1, 28)], "bpm"),
        "training_load": _points("training_load", [20] + [30 + index % 3 for index in range(1, 36)], "load"),
    }
    raw.sleep_by_day = {
        point.day: {"date": point.day, "sleep_duration": int(point.value)}
        for point in raw.series["sleep_duration"]
    }
    raw.training_by_day = {
        point.day: {
            "date": point.day,
            "total_load": point.value,
            "total_duration": 30,
            "workout_count": 1,
        }
        for point in raw.series["training_load"]
    }
    raw.training_history_coverage = {
        "status": "COMPLETE",
        "verified_days": [
            (TARGET - timedelta(days=offset)).isoformat() for offset in range(36)
        ],
        "prior_7d_verified": True,
    }
    return AnalysisDataset(raw=raw)


def _request():
    return AnalysisRequest(
        user_id="deterministic-user",
        target_date=TARGET,
        analysis_run_id="fixed-analysis-run",
        as_of=AS_OF,
        timezone="Asia/Shanghai",
        profile_revision=3,
        input_revision=7,
        config_digest="fixed-policy-digest",
    )


def test_fixed_dataset_request_policy_is_byte_stable():
    policy = AnalysisPolicy(timezone="Asia/Shanghai")

    first = analyze(_dataset(), _request(), policy)
    second = analyze(_dataset(), _request(), policy)

    assert first.model_dump_json() == second.model_dump_json()
    assert first.daily.analysis_run_id == first.weekly.analysis_run_id
    assert first.weekly.analysis_run_id == first.monthly.analysis_run_id
    assert first.daily.generated_at == AS_OF
    assert first.weekly.generated_at == AS_OF
    assert first.monthly.generated_at == AS_OF
    assert first.personal_model.generated_at == AS_OF
    assert first.personal_associations.generated_at == AS_OF
    assert first.morning_briefing.generated_at == AS_OF


def test_explicit_timezone_and_as_of_do_not_use_process_configuration(monkeypatch):
    import vitalis.config

    monkeypatch.setattr(vitalis.config, "settings", None)
    result = analyze(
        _dataset(),
        _request(),
        AnalysisPolicy(timezone="Asia/Shanghai"),
    )

    assert result.daily.report_context["timezone"] == "Asia/Shanghai"
    assert result.daily.report_context["as_of"] == AS_OF.isoformat()
