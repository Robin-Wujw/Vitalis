"""Explicit intelligence commands, read-only queries, and user actions."""

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from vitalis.storage import HealthRepository, session_scope
from vitalis.time import local_today

from .analyzers import HrvAnalyzer, RecoveryAnalyzer, SleepAnalyzer, TrainingAnalyzer, build_states
from .baseline import BaselineEngine
from .contracts import (
    AgentContext,
    AnalysisResult,
    AnalysisRun,
    AnalysisRunStatus,
    DailyProfile,
    DecisionExplanation,
    EvidenceRef,
    ExplanationFact,
    HealthEvent,
    HealthEventResponse,
    HealthTimeline,
    MonthlyProfile,
    ProfileFeatures,
    PersonalModel,
    PersonalAssociationProfile,
    RecommendationInstance,
    SubjectiveFeedback,
    SubjectiveFeedbackInput,
    TrendResponse,
    TrainingResponseProfile,
    WeeklyProfile,
)
from .decision import DecisionEngine
from .association import PersonalAssociationEngine
from .context import AgentContextEngine
from .events import HealthEventEngine
from .lifecycle import EventLifecycleEngine
from .profile import ProfileLoader
from .personal import PersonalModelEngine
from .monthly import MonthlyProfileEngine
from .training_response import TrainingResponseEngine
from .timeline import HealthTimelineEngine
from .trend import TrendEngine
from .weekly import WeeklyProfileEngine


EVIDENCE_REFS = [
    EvidenceRef(
        id="WHO_PHYSICAL_ACTIVITY",
        title="WHO physical activity fact sheet",
        url="https://www.who.int/europe/news-room/fact-sheets/item/physical-activity",
        applies_to=["weekly_training_balance"],
    ),
    EvidenceRef(
        id="HRV_STANDARDS_1996",
        title="Heart rate variability: standards of measurement and interpretation",
        url="https://pubmed.ncbi.nlm.nih.gov/8737210/",
        applies_to=["hrv_measurement", "ln_rmssd"],
    ),
    EvidenceRef(
        id="ARM_PPG_OH1_2019",
        title="Validation of Polar OH1 optical heart rate sensor during exercise",
        url="https://pubmed.ncbi.nlm.nih.gov/31120968/",
        applies_to=["upper_arm_ppg_heart_rate", "measurement_site"],
    ),
    EvidenceRef(
        id="ARM_WRIST_PPG_2025",
        title="Wrist-worn and arm-worn wearables for monitoring heart rate",
        url="https://pubmed.ncbi.nlm.nih.gov/40116771/",
        applies_to=["upper_arm_ppg_heart_rate", "measurement_site"],
    ),
    EvidenceRef(
        id="WRIST_PPG_META_2020",
        title="Validity of wrist-worn PPG devices to measure heart rate",
        url="https://doi.org/10.1080/02640414.2020.1767348",
        applies_to=["wrist_ppg_heart_rate", "activity_specific_accuracy"],
    ),
    EvidenceRef(
        id="PPG_ERROR_SOURCES_2020",
        title="Investigating sources of inaccuracy in wearable optical heart rate sensors",
        url="https://doi.org/10.1038/s41746-020-0226-6",
        applies_to=["ppg_motion_artifact", "device_specific_accuracy"],
    ),
    EvidenceRef(
        id="PRV_HRV_REVIEW_2013",
        title="How accurate is pulse rate variability as an estimate of heart rate variability?",
        url="https://doi.org/10.1016/j.ijcard.2012.03.119",
        applies_to=["ppg_hrv_limitations", "device_fusion"],
    ),
    EvidenceRef(
        id="WSS_WEARABLE_SLEEP_2025",
        title="World Sleep Society recommendations for consumer sleep trackers",
        url="https://pubmed.ncbi.nlm.nih.gov/40300398/",
        applies_to=["sleep_stage_limitations"],
    ),
    EvidenceRef(
        id="AASM_SLEEP_DURATION",
        title="Recommended amount of sleep for a healthy adult",
        url="https://aasm.org/resources/pdf/adultsleepdurationconsensus.pdf",
        applies_to=["sleep_duration"],
    ),
    EvidenceRef(
        id="IOC_LOAD_2016",
        title="IOC consensus statement on load in sport and risk of injury",
        url="https://pubmed.ncbi.nlm.nih.gov/27535989/",
        applies_to=["integrated_load_monitoring"],
    ),
]


class IntelligenceCommand:
    """Run deterministic analysis and persist an immutable result set."""

    def analyze(self, user_id: str, day: date | None = None) -> AnalysisResult:
        target = day or local_today()
        run = AnalysisRun(
            id=uuid4().hex,
            user_id=user_id,
            target_date=target,
            status=AnalysisRunStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )
        with session_scope() as db:
            repo = HealthRepository(db)
            repo.upsert_user(user_id)
            repo.create_analysis_run(run)

        try:
            with session_scope() as db:
                repo = HealthRepository(db)
                raw = ProfileLoader(repo).load(user_id, target)
                identity = repo.identity_context(user_id)
                response_feedback = repo.subjective_feedback(
                    user_id, target - timedelta(days=179), target
                )
                weekly_feedback = [
                    item.model_dump(mode="json")
                    for item in response_feedback
                    if target - timedelta(days=6) <= item.date <= target
                ]
                monthly_feedback = [
                    item.model_dump(mode="json")
                    for item in response_feedback
                    if target - timedelta(days=27) <= item.date <= target
                ]
                recommendation_by_workout = repo.recommendations_for_workouts(
                    user_id,
                    [item["workout_id"] for item in raw.workouts if item.get("workout_id")],
                )

            daily = self._build_daily_from_raw(run.id, raw, identity)
            training_responses = TrainingResponseEngine().build(
                run.id,
                raw,
                response_feedback,
                recommendation_by_workout,
            )
            response_profile = TrainingResponseProfile(
                analysis_run_id=run.id,
                user_id=user_id,
                date=target,
                responses=training_responses,
            )
            association_profile = PersonalAssociationEngine().build(run.id, raw)
            personal_model = PersonalModelEngine().build(
                run.id,
                daily,
                training_responses,
                association_profile.associations,
            )
            recommendation = RecommendationInstance(
                id=daily.decision.recommendation_id,
                analysis_run_id=run.id,
                user_id=user_id,
                date=target,
                decision=daily.decision,
                created_at=daily.generated_at,
            )
            with session_scope() as db:
                repo = HealthRepository(db)
                daily.events = EventLifecycleEngine().reconcile(
                    repo, run.id, user_id, target, daily.events
                )
                weekly_events = repo.health_events(
                    user_id, target - timedelta(days=6), target
                )
                weekly = WeeklyProfileEngine().build(
                    run.id,
                    raw,
                    daily.trends,
                    weekly_events,
                    feedback=weekly_feedback,
                    evidence_refs=EVIDENCE_REFS,
                )
                monthly_events = repo.health_events(
                    user_id, target - timedelta(days=27), target
                )
                monthly = MonthlyProfileEngine().build(
                    run.id,
                    raw,
                    daily.trends,
                    monthly_events,
                    association_profile.associations,
                    feedback=monthly_feedback,
                    evidence_refs=EVIDENCE_REFS,
                )
                repo.save_recommendation(recommendation)
                self._save_snapshot(repo, run.id, daily, "daily", target, target)
                self._save_snapshot(
                    repo,
                    run.id,
                    weekly,
                    "weekly",
                    weekly.period_start,
                    weekly.period_end,
                )
                self._save_snapshot(
                    repo,
                    run.id,
                    monthly,
                    "monthly",
                    monthly.period_start,
                    monthly.period_end,
                )
                self._save_snapshot(
                    repo,
                    run.id,
                    response_profile,
                    "training_responses",
                    target - timedelta(days=89),
                    target,
                )
                self._save_snapshot(
                    repo,
                    run.id,
                    association_profile,
                    "personal_associations",
                    target - timedelta(days=89),
                    target,
                )
                self._save_snapshot(
                    repo,
                    run.id,
                    personal_model,
                    "personal_model",
                    target,
                    target,
                )
                row = repo.complete_analysis_run(run.id, AnalysisRunStatus.SUCCEEDED.value)
                completed_run = _run_from_row(row)
            return AnalysisResult(
                run=completed_run,
                daily=daily,
                weekly=weekly,
                monthly=monthly,
                recommendation=recommendation,
                training_responses=training_responses,
                personal_model=personal_model,
                personal_associations=association_profile,
            )
        except Exception as exc:
            with session_scope() as db:
                HealthRepository(db).complete_analysis_run(
                    run.id, AnalysisRunStatus.FAILED.value, str(exc)
                )
            raise

    @staticmethod
    def _save_snapshot(repo, run_id, profile, profile_type, period_start, period_end):
        repo.save_analysis_snapshot(
            run_id,
            profile.user_id,
            profile_type,
            period_start,
            period_end,
            profile.schema_version,
            profile.intelligence_version,
            profile.decision_policy_version,
            profile.evidence_version,
            profile.model_dump(mode="json"),
        )

    @staticmethod
    def _build_daily_from_raw(analysis_run_id: str, raw, identity: dict) -> DailyProfile:
        target = raw.day
        baselines = BaselineEngine().build(raw.series, target)
        sleep, sleep_state = SleepAnalyzer().analyze(raw, baselines)
        hrv = HrvAnalyzer().analyze(raw, baselines)
        training = TrainingAnalyzer().analyze(raw, baselines)
        recovery = RecoveryAnalyzer().analyze(raw, sleep, sleep_state, hrv, training)
        decision = DecisionEngine().decide(
            uuid4().hex, sleep_state, hrv, recovery, training
        )
        trends = TrendEngine().calculate(raw)
        events = HealthEventEngine().detect(raw, baselines, trends, hrv, recovery)
        return DailyProfile(
            analysis_run_id=analysis_run_id,
            user_id=raw.user_id,
            date=target,
            data_quality=raw.data_quality,
            facts=raw.facts,
            baselines=baselines,
            features=ProfileFeatures(sleep=sleep, hrv=hrv, recovery=recovery, training=training),
            trends=trends,
            events=events,
            states=build_states(sleep_state, recovery, training),
            decision=decision,
            evidence_refs=EVIDENCE_REFS,
            metadata={
                "identity": identity,
                "baseline_policy": {
                    "windows_days": [7, 28],
                    "minimum_distinct_days": {"7": 3, "28": 14},
                    "policy_type": "product_policy_not_medical_threshold",
                },
                "diagnostic_use": False,
            },
        )


class IntelligenceQuery:
    """Read already persisted intelligence without triggering analysis."""

    def daily(self, user_id: str, day: date | None = None) -> DailyProfile | None:
        target = day or local_today()
        with session_scope() as db:
            row = HealthRepository(db).latest_analysis_snapshot(user_id, "daily", target)
            return DailyProfile.model_validate(row.payload) if row else None

    def weekly(self, user_id: str, day: date | None = None) -> WeeklyProfile | None:
        target = day or local_today()
        with session_scope() as db:
            row = HealthRepository(db).latest_analysis_snapshot(user_id, "weekly", target)
            return WeeklyProfile.model_validate(row.payload) if row else None

    def monthly(self, user_id: str, day: date | None = None) -> MonthlyProfile | None:
        target = day or local_today()
        with session_scope() as db:
            row = HealthRepository(db).latest_analysis_snapshot(user_id, "monthly", target)
            return MonthlyProfile.model_validate(row.payload) if row else None

    def trends(self, user_id: str, day: date | None = None) -> TrendResponse | None:
        daily = self.daily(user_id, day)
        if daily is None:
            return None
        return TrendResponse(
            user_id=user_id,
            date=daily.date,
            generated_at=daily.generated_at,
            trends=daily.trends,
        )

    def events(
        self,
        user_id: str,
        start: date,
        end: date,
        event_type: str | None = None,
    ) -> HealthEventResponse:
        if start > end:
            raise ValueError("开始日期不能晚于结束日期")
        with session_scope() as db:
            events = HealthRepository(db).health_events(user_id, start, end, event_type)
        return HealthEventResponse(
            user_id=user_id,
            period_start=start,
            period_end=end,
            events=events,
        )

    def explain(self, user_id: str, day: date | None = None) -> DecisionExplanation | None:
        profile = self.daily(user_id, day)
        if profile is None:
            return None
        facts: list[ExplanationFact] = []
        sleep = profile.features.sleep
        hrv = profile.features.hrv
        training = profile.features.training
        if sleep.duration_minutes is not None:
            facts.append(ExplanationFact(
                code="sleep_duration", label="睡眠时长", value=sleep.duration_minutes, unit="分钟"
            ))
        if hrv.value_ms is not None:
            facts.append(ExplanationFact(
                code=hrv.preferred_metric or "hrv", label="心率变异性", value=hrv.value_ms, unit="毫秒"
            ))
        if hrv.rhr_bpm is not None:
            facts.append(ExplanationFact(
                code="resting_hr", label="静息心率", value=hrv.rhr_bpm, unit="次/分钟"
            ))
        if training.load_7d is not None:
            facts.append(ExplanationFact(
                code="training_load_7d", label="近 7 天训练负荷", value=training.load_7d
            ))
        return DecisionExplanation(
            user_id=user_id,
            date=profile.date,
            facts=facts,
            inferences=profile.decision.driver_labels,
            limitations=profile.decision.limitation_labels,
            action=profile.decision,
            evidence_refs=profile.evidence_refs,
        )

    def context(self, user_id: str, day: date | None = None) -> AgentContext | None:
        target = day or local_today()
        daily = self.daily(user_id, target)
        weekly = self.weekly(user_id, target)
        personal_model = self.personal_model(user_id, target)
        if daily is None or weekly is None or personal_model is None:
            return None
        with session_scope() as db:
            repo = HealthRepository(db)
            feedback = repo.subjective_feedback(user_id, target - timedelta(days=6), target)
            events = repo.active_health_events(user_id)
        return AgentContextEngine().build(
            daily, weekly, events, feedback, personal_model
        )

    def timeline(
        self,
        user_id: str,
        start: date,
        end: date,
        limit: int = 100,
    ) -> HealthTimeline:
        if start > end:
            raise ValueError("开始日期不能晚于结束日期")
        with session_scope() as db:
            repo = HealthRepository(db)
            row = repo.latest_analysis_snapshot_on_or_before(
                user_id, "training_responses", end
            )
            response_profile = (
                TrainingResponseProfile.model_validate(row.payload) if row else None
            )
            monthly_row = repo.latest_analysis_snapshot_on_or_before(
                user_id, "monthly", end
            )
            monthly = MonthlyProfile.model_validate(monthly_row.payload) if monthly_row else None
            association_row = repo.latest_analysis_snapshot_on_or_before(
                user_id, "personal_associations", end
            )
            associations = (
                PersonalAssociationProfile.model_validate(association_row.payload)
                if association_row else None
            )
            return HealthTimelineEngine().build(
                repo,
                user_id,
                start,
                end,
                response_profile,
                monthly,
                associations,
                limit,
            )

    def feedback(
        self,
        user_id: str,
        start: date,
        end: date,
    ) -> list[SubjectiveFeedback]:
        with session_scope() as db:
            return HealthRepository(db).subjective_feedback(user_id, start, end)

    def recommendation(
        self, user_id: str, recommendation_id: str
    ) -> RecommendationInstance | None:
        with session_scope() as db:
            return HealthRepository(db).recommendation(user_id, recommendation_id)

    def training_responses(
        self, user_id: str, day: date | None = None
    ) -> TrainingResponseProfile | None:
        target = day or local_today()
        with session_scope() as db:
            row = HealthRepository(db).latest_analysis_snapshot(
                user_id, "training_responses", target
            )
            return TrainingResponseProfile.model_validate(row.payload) if row else None

    def personal_model(
        self, user_id: str, day: date | None = None
    ) -> PersonalModel | None:
        target = day or local_today()
        with session_scope() as db:
            row = HealthRepository(db).latest_analysis_snapshot(
                user_id, "personal_model", target
            )
            return PersonalModel.model_validate(row.payload) if row else None

    def personal_associations(
        self, user_id: str, day: date | None = None
    ) -> PersonalAssociationProfile | None:
        target = day or local_today()
        with session_scope() as db:
            row = HealthRepository(db).latest_analysis_snapshot(
                user_id, "personal_associations", target
            )
            return PersonalAssociationProfile.model_validate(row.payload) if row else None



class IntelligenceAction:
    """Persist explicit user actions without running health analysis."""

    def acknowledge_event(self, user_id: str, event_id: str) -> HealthEvent | None:
        with session_scope() as db:
            return HealthRepository(db).acknowledge_health_event(user_id, event_id)

    def complete_recommendation(
        self, user_id: str, recommendation_id: str, workout_id: str
    ) -> RecommendationInstance:
        with session_scope() as db:
            return HealthRepository(db).link_recommendation(
                user_id, recommendation_id, workout_id
            )

    def log_feedback(
        self,
        user_id: str,
        feedback_input: SubjectiveFeedbackInput,
    ) -> SubjectiveFeedback:
        target = feedback_input.date or local_today()
        with session_scope() as db:
            repo = HealthRepository(db)
            if feedback_input.workout_id and not repo.workout(user_id, feedback_input.workout_id):
                raise ValueError("指定训练不存在或不属于当前用户")
            if feedback_input.recommendation_id:
                recommendation = repo.recommendation(
                    user_id, feedback_input.recommendation_id
                )
                if recommendation is None:
                    raise ValueError("训练建议不存在或不属于当前用户")
                if recommendation.linked_workout_id != feedback_input.workout_id:
                    raise ValueError("训练反馈与建议关联的训练不一致")
            feedback = SubjectiveFeedback(
                id=uuid4().hex,
                user_id=user_id,
                date=target,
                **feedback_input.model_dump(exclude={"date"}),
            )
            return repo.save_subjective_feedback(feedback)

def _run_from_row(row) -> AnalysisRun:
    return AnalysisRun(
        id=row.id,
        user_id=row.user_id,
        target_date=row.target_date,
        status=AnalysisRunStatus(row.status),
        started_at=row.started_at.replace(tzinfo=timezone.utc),
        completed_at=(
            row.completed_at.replace(tzinfo=timezone.utc) if row.completed_at else None
        ),
        intelligence_version=row.intelligence_version,
        decision_policy_version=row.decision_policy_version,
        evidence_version=row.evidence_version,
        error=row.error,
    )
