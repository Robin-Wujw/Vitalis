"""Application service assembling a complete DailyProfile."""

from datetime import date, timedelta
from uuid import uuid4

from vitalis.storage import HealthRepository, session_scope
from vitalis.time import local_today

from .analyzers import HrvAnalyzer, RecoveryAnalyzer, SleepAnalyzer, TrainingAnalyzer, build_states
from .baseline import BaselineEngine
from .contracts import (
    AgentContext,
    DailyProfile,
    DecisionExplanation,
    EvidenceRef,
    ExplanationFact,
    HealthEvent,
    HealthEventResponse,
    ProfileFeatures,
    SubjectiveFeedback,
    SubjectiveFeedbackInput,
    TrendResponse,
    WeeklyProfile,
)
from .decision import DecisionEngine
from .events import HealthEventEngine
from .profile import ProfileLoader
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


class IntelligencePipeline:
    def build_daily_profile(self, user_id: str, day: date | None = None) -> DailyProfile:
        target = day or local_today()
        with session_scope() as db:
            repo = HealthRepository(db)
            raw = ProfileLoader(repo).load(user_id, target)
            identity = repo.identity_context(user_id)

        profile = self._build_daily_from_raw(raw, identity)
        profile.events = self._persist_events(user_id, target, profile.events)
        self._persist_snapshot(
            user_id,
            "daily",
            target,
            target,
            profile.schema_version,
            profile.model_version,
            profile.model_dump(mode="json"),
        )
        return profile

    def build_weekly_profile(self, user_id: str, day: date | None = None) -> WeeklyProfile:
        target = day or local_today()
        with session_scope() as db:
            repo = HealthRepository(db)
            raw = ProfileLoader(repo).load(user_id, target)
            feedback = [
                item.model_dump(mode="json")
                for item in repo.subjective_feedback(user_id, target - timedelta(days=6), target)
            ]
        daily = self._build_daily_from_raw(raw, identity={})
        self._persist_events(user_id, target, daily.events)
        with session_scope() as db:
            events = HealthRepository(db).health_events(
                user_id, target - timedelta(days=6), target
            )
        profile = WeeklyProfileEngine().build(
            raw,
            daily.trends,
            events,
            feedback=feedback,
            evidence_refs=EVIDENCE_REFS,
        )
        self._persist_snapshot(
            user_id,
            "weekly",
            profile.period_start,
            profile.period_end,
            profile.schema_version,
            profile.model_version,
            profile.model_dump(mode="json"),
        )
        return profile

    def trends(self, user_id: str, day: date | None = None):
        target = day or local_today()
        with session_scope() as db:
            raw = ProfileLoader(HealthRepository(db)).load(user_id, target)
        return TrendResponse(user_id=user_id, date=target, trends=TrendEngine().calculate(raw))

    def events(
        self,
        user_id: str,
        start: date,
        end: date,
        event_type: str | None = None,
    ) -> HealthEventResponse:
        if start > end:
            raise ValueError("开始日期不能晚于结束日期")
        self.build_daily_profile(user_id, end)
        with session_scope() as db:
            events = HealthRepository(db).health_events(user_id, start, end, event_type)
        return HealthEventResponse(
            user_id=user_id,
            period_start=start,
            period_end=end,
            events=events,
        )

    def explain(self, user_id: str, day: date | None = None) -> DecisionExplanation:
        profile = self.build_daily_profile(user_id, day)
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

    def context(self, user_id: str, day: date | None = None) -> AgentContext:
        target = day or local_today()
        daily = self.build_daily_profile(user_id, target)
        weekly = self.build_weekly_profile(user_id, target)
        with session_scope() as db:
            repo = HealthRepository(db)
            feedback = repo.subjective_feedback(user_id, target - timedelta(days=6), target)
            events = [
                event for event in repo.health_events(user_id, target - timedelta(days=27), target)
                if not event.acknowledged
            ]
        return AgentContext(
            user_id=user_id,
            date=target,
            daily=daily,
            weekly=weekly,
            unacknowledged_events=events,
            recent_feedback=feedback,
        )

    def acknowledge_event(self, user_id: str, event_id: str) -> HealthEvent | None:
        with session_scope() as db:
            return HealthRepository(db).acknowledge_health_event(user_id, event_id)

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
            feedback = SubjectiveFeedback(
                id=uuid4().hex,
                user_id=user_id,
                date=target,
                **feedback_input.model_dump(exclude={"date"}),
            )
            return repo.save_subjective_feedback(feedback)

    def feedback(
        self,
        user_id: str,
        start: date,
        end: date,
    ) -> list[SubjectiveFeedback]:
        with session_scope() as db:
            return HealthRepository(db).subjective_feedback(user_id, start, end)

    @staticmethod
    def _build_daily_from_raw(raw, identity: dict) -> DailyProfile:
        target = raw.day
        baselines = BaselineEngine().build(raw.series, target)
        sleep, sleep_state = SleepAnalyzer().analyze(raw, baselines)
        hrv = HrvAnalyzer().analyze(raw, baselines)
        training = TrainingAnalyzer().analyze(raw, baselines)
        recovery = RecoveryAnalyzer().analyze(raw, sleep, sleep_state, hrv, training)
        decision = DecisionEngine().decide(sleep_state, hrv, recovery, training)
        trends = TrendEngine().calculate(raw)
        events = HealthEventEngine().detect(raw, baselines, trends, hrv, recovery)
        profile = DailyProfile(
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
        return profile

    @staticmethod
    def _persist_events(user_id: str, target: date, events):
        if events:
            with session_scope() as db:
                repo = HealthRepository(db)
                repo.save_health_events(user_id, events)
                stored = {item.id: item for item in repo.health_events(user_id, target, target)}
            return [stored.get(item.id, item) for item in events]
        return []

    @staticmethod
    def _persist_snapshot(
        user_id: str,
        profile_type: str,
        period_start: date,
        period_end: date,
        schema_version: str,
        model_version: str,
        payload: dict,
    ) -> None:
        with session_scope() as db:
            HealthRepository(db).save_analysis_snapshot(
                user_id,
                profile_type,
                period_start,
                period_end,
                schema_version,
                model_version,
                payload,
            )
