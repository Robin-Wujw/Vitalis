from datetime import date, datetime, timedelta, timezone

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.adapters.persistence.models import AnalysisRun, AnalysisSnapshot
from vitalis.adapters.persistence.repositories import _current_analysis_config_digest
from vitalis.application.analysis import (
    AnalysisDataset, AnalysisPolicy, AnalysisRequest, analyze, build_report_projections,
)
from vitalis.intelligence.contracts import (
    DAILY_SCHEMA_VERSION, WEEKLY_SCHEMA_VERSION, DECISION_POLICY_VERSION,
    EVIDENCE_VERSION, INTELLIGENCE_VERSION, ConfidenceBand, DataQuality,
    EventSeverity, HealthEvent, QualityStatus, TrainingPreferences,
)
from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint


TARGET = date(2026, 8, 28)
AS_OF = datetime(2026, 8, 29, 3, 0, tzinfo=timezone.utc)


def _dataset():
    raw = RawDailyProfile(
        user_id="report-consistency-user",
        day=TARGET,
        as_of=AS_OF,
        timezone_name="Asia/Shanghai",
    )
    raw.training_preferences = TrainingPreferences(user_id=raw.user_id)
    raw.data_quality = DataQuality(status=QualityStatus.INSUFFICIENT, status_label="数据不足")

    def points(metric, values, unit):
        return [
            SeriesPoint(
                metric=metric,
                value=value,
                unit=unit,
                day=TARGET - timedelta(days=index),
                observed_at=TARGET - timedelta(days=index),
                source="zepp",
                source_scope="normalized_daily_record",
            )
            for index, value in enumerate(values)
        ]

    raw.series = {
        "sleep_duration": points("sleep_duration", [420] * 28, "min"),
        "hrv_rmssd": points("hrv_rmssd", [45 + index % 3 for index in range(28)], "ms"),
        "resting_hr": points("resting_hr", [58 + index % 2 for index in range(28)], "bpm"),
        "training_load": points("training_load", [20] * 36, "load"),
    }
    raw.sleep_by_day = {
        point.day: {"date": point.day, "sleep_duration": int(point.value)}
        for point in raw.series["sleep_duration"]
    }
    raw.training_by_day = {
        point.day: {
            "date": point.day,
            "total_load": point.value,
            "total_duration": 20,
            "workout_count": 1,
        }
        for point in raw.series["training_load"]
    }
    raw.training_history_coverage = {
        "status": "COMPLETE",
        "verified_days": [
            (TARGET - timedelta(days=index)).isoformat() for index in range(36)
        ],
        "prior_7d_verified": True,
    }
    return AnalysisDataset(raw=raw)


def test_reports_share_one_canonical_run_and_facts():
    result = analyze(
        _dataset(),
        AnalysisRequest(
            user_id="report-consistency-user",
            target_date=TARGET,
            analysis_run_id="report-run",
            as_of=AS_OF,
            timezone="Asia/Shanghai",
        ),
        AnalysisPolicy(timezone="Asia/Shanghai"),
    )

    assert result.run.id == "report-run"
    assert result.daily.analysis_run_id == result.run.id
    assert result.weekly.analysis_run_id == result.run.id
    assert result.monthly.analysis_run_id == result.run.id
    assert result.morning_briefing.analysis_run_id == result.run.id
    assert result.morning_briefing.date == result.daily.date
    assert result.weekly.period_end == TARGET
    assert (result.weekly.period_end - result.weekly.period_start).days == 6
    assert result.monthly.period_end == TARGET
    assert (result.monthly.period_end - result.monthly.period_start).days == 27
    assert result.morning_briefing.decision_action == result.daily.decision.action
    assert result.morning_briefing.action_plan == result.daily.decision.action_plan


def test_final_event_projection_rebuilds_period_recovery_recommendations():
    dataset = _dataset()
    result = analyze(
        dataset,
        AnalysisRequest(
            user_id="report-consistency-user", target_date=TARGET,
            analysis_run_id="report-events-run", as_of=AS_OF,
            timezone="Asia/Shanghai",
        ),
        AnalysisPolicy(timezone="Asia/Shanghai"),
    )
    event = HealthEvent(
        id="synthetic-recovery-event", type="HRV_DROP", type_label="HRV 下降",
        severity=EventSeverity.MODERATE, severity_label="中等",
        metric="hrv_rmssd", metric_label="HRV",
        start_date=TARGET, end_date=TARGET, duration_days=1,
        confidence=ConfidenceBand.HIGH, confidence_label="较高", summary="合成事件",
    )
    finalized_daily = result.daily.model_copy(update={"events": [event]})
    weekly, monthly, morning = build_report_projections(
        result.run.id, dataset.raw, finalized_daily,
        result.personal_associations.associations, [], [], (),
        result.open_health_insights,
    )
    assert weekly.inferences.events == [event]
    assert monthly.inferences.events == [event]
    assert "PRIORITIZE_RECOVERY" in {
        item.code for item in weekly.actions.recommendations
    }
    assert "MONTHLY_PRIORITIZE_RECOVERY" in {
        item.code for item in monthly.actions.recommendations
    }
    assert morning.analysis_run_id == result.run.id


def test_latest_snapshots_with_tied_generation_time_select_one_completed_run():
    user_id = "report-snapshot-tie-user"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        digest = _current_analysis_config_digest()
        for run_id, minute in (("run-tie-a", 1), ("run-tie-b", 2)):
            db.add(AnalysisRun(
                id=run_id, user_id=user_id, target_date=TARGET,
                status="SUCCEEDED", started_at=AS_OF.replace(tzinfo=None),
                completed_at=(AS_OF + timedelta(minutes=minute)).replace(tzinfo=None),
                intelligence_version=INTELLIGENCE_VERSION,
                decision_policy_version=DECISION_POLICY_VERSION,
                evidence_version=EVIDENCE_VERSION,
                profile_revision_used=0, input_revision_used=0,
                config_digest=digest,
            ))
        db.flush()
        for profile_type, schema in (
            ("daily", DAILY_SCHEMA_VERSION), ("weekly", WEEKLY_SCHEMA_VERSION),
            ("personal_model", "2.0"),
        ):
            for run_id, snapshot_id in (
                ("run-tie-a", "z" if profile_type == "daily" else "a"),
                ("run-tie-b", "a" if profile_type == "daily" else "z"),
            ):
                db.add(AnalysisSnapshot(
                    id=f"{snapshot_id}-{profile_type}-{run_id}",
                    analysis_run_id=run_id, user_id=user_id,
                    profile_type=profile_type, period_start=TARGET, period_end=TARGET,
                    schema_version=schema,
                    intelligence_version=INTELLIGENCE_VERSION,
                    decision_policy_version=DECISION_POLICY_VERSION,
                    evidence_version=EVIDENCE_VERSION,
                    generated_at=AS_OF.replace(tzinfo=None), payload={},
                ))
        db.flush()
        assert {
            repo.latest_analysis_snapshot(user_id, kind, TARGET).analysis_run_id
            for kind in ("daily", "weekly", "personal_model")
        } == {"run-tie-b"}
        assert {
            repo.latest_analysis_snapshot_on_or_before(user_id, kind, TARGET).analysis_run_id
            for kind in ("daily", "weekly", "personal_model")
        } == {"run-tie-b"}
