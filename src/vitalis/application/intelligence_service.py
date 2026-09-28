"""Explicit intelligence commands, read-only queries, and user actions."""

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from typing import Callable
from uuid import uuid4

from vitalis.application.analysis import (
    AnalysisDataset,
    AnalysisPolicy,
    AnalysisRequest,
    analyze_with_trace,
    build_daily_profile,
    build_report_projections,
)
from vitalis.application.analysis_policy import (
    analysis_policy_digest,
    installed_analysis_rules_digest,
)
from vitalis.application.ports import (
    AnalysisJobRepository,
    IntelligenceUowFactory,
    JobClaim,
)
from vitalis.intelligence.evidence import EVIDENCE_REFS

from vitalis.intelligence.context import AgentContextEngine
from vitalis.intelligence.contracts import (
    AgentContext,
    AnalysisResult,
    AnalysisRun,
    AnalysisRunStatus,
    DailyProfile,
    DECISION_EXPLANATION_SCHEMA_VERSION,
    DecisionExplanation,
    ExplanationSnapshotProvenance,
    HealthEvent,
    HealthEventResponse,
    HealthTimeline,
    MonthlyProfile,
    MorningBriefing,
    PersonalModel,
    PersonalAssociationProfile,
    RecommendationInstance,
    ReportBriefing,
    StrengthExerciseRecord,
    StrengthWorkoutConfirmationInput,
    SubjectiveFeedback,
    SubjectiveFeedbackInput,
    TrendResponse,
    TrainingPreferenceInput,
    TrainingPreferencePatch,
    TrainingPreferences,
    TrainingResponseProfile,
    UserProfile,
    UserProfilePatch,
    ProfileRevisionConflict,
    WeeklyProfile,
)
from vitalis.intelligence.evening_briefing import EveningBriefingEngine
from vitalis.intelligence.lifecycle import EventLifecycleEngine
from vitalis.intelligence.monthly_briefing import MonthlyBriefingEngine
from vitalis.intelligence.morning_briefing import MorningBriefingEngine
from vitalis.intelligence.profile import ProfileLoader
from vitalis.intelligence.strength import normalize_exercise
from vitalis.intelligence.timeline import HealthTimelineEngine
from vitalis.intelligence.weekly_briefing import WeeklyBriefingEngine


def _feedback_request_hash(feedback_input: SubjectiveFeedbackInput) -> str:
    """Hash the validated request body using the current idempotency contract."""
    payload = json.dumps(
        feedback_input.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class IntelligenceCommand:
    """Run deterministic analysis and persist an immutable result set."""

    def __init__(
        self,
        uow_factory: IntelligenceUowFactory,
        *,
        timezone_name: str,
        catalog_revision: str,
        today_factory: Callable[[], date],
        now_factory: Callable[[], datetime],
    ) -> None:
        self._uow_factory = uow_factory
        self._timezone = timezone_name
        self._catalog_revision = catalog_revision
        self._today_factory = today_factory
        self._now_factory = now_factory

    def analyze(
        self, user_id: str, day: date | None = None, *,
        job_claim: JobClaim | None = None,
        job_repository: AnalysisJobRepository | None = None,
    ) -> AnalysisResult:
        target = day or self._today_factory()
        if (job_claim is None) != (job_repository is None):
            raise ValueError("job claim and repository must be provided together")
        if job_claim is not None and (
            job_claim.user_id != user_id or job_claim.target_date != target
        ):
            raise ValueError("job claim does not match analysis request")
        run = AnalysisRun(
            id=uuid4().hex,
            user_id=user_id,
            target_date=target,
            status=AnalysisRunStatus.RUNNING,
            started_at=self._now_factory(),
        )
        try:
            with self._uow_factory() as uow:
                repo = uow.repository
                if job_claim is not None:
                    if not repo.user_exists(user_id):
                        raise ValueError("user not found")
                else:
                    repo.upsert_user(user_id)
                profile = repo.user_profile(user_id)
                run.profile_revision_used = profile.revision
                run.input_revision_used = repo.analysis_input_revision(user_id)
                run.config_digest = analysis_policy_digest(
                    self._timezone,
                    run.intelligence_version,
                    run.decision_policy_version,
                    run.evidence_version,
                    self._catalog_revision,
                    installed_analysis_rules_digest(),
                )
                repo.create_analysis_run(run)
                if job_claim is not None and not job_repository.attach_run(
                    uow.transaction, job_claim, run.id
                ):
                    raise RuntimeError("analysis job claim is no longer current")
                uow.commit()

            with self._uow_factory() as uow:
                repo = uow.repository
                raw = ProfileLoader(repo).load(
                    user_id,
                    target,
                    profile=profile,
                    as_of=run.started_at,
                    timezone_name=self._timezone,
                )
                identity = repo.identity_context(user_id)
                response_feedback = repo.subjective_feedback(
                    user_id, target - timedelta(days=179), target
                )
                for item in response_feedback:
                    if item.workout_id and item.workout_source:
                        raw.feedback_by_workout.setdefault(
                            (item.workout_source, item.workout_id), []
                        ).append(item)
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
                recommendation_by_workout = repo.recommendations_for_workout_keys(
                    user_id,
                    [
                        (str(item.get("source") or "zepp"), item["workout_id"])
                        for item in raw.workouts if item.get("workout_id")
                    ],
                )
                prior_events = repo.active_health_events(user_id)

            trace = analyze_with_trace(
                AnalysisDataset(
                    raw=raw,
                    identity=identity,
                    response_feedback=tuple(response_feedback),
                    weekly_feedback=tuple(weekly_feedback),
                    monthly_feedback=tuple(monthly_feedback),
                    recommendation_by_workout=recommendation_by_workout,
                    prior_events=tuple(prior_events),
                ),
                AnalysisRequest(
                    user_id=user_id,
                    target_date=target,
                    analysis_run_id=run.id,
                    as_of=run.started_at,
                    timezone=self._timezone,
                    profile_revision=run.profile_revision_used or 0,
                    input_revision=run.input_revision_used,
                    config_digest=run.config_digest,
                ),
                AnalysisPolicy(
                    timezone=self._timezone,
                    evidence_refs=tuple(EVIDENCE_REFS),
                ),
                daily_builder=self._build_daily_from_raw,
            )
            computed = trace.result
            daily = computed.daily
            weekly = computed.weekly
            monthly = computed.monthly
            recommendation = computed.recommendation
            training_responses = computed.training_responses
            association_profile = computed.personal_associations
            personal_model = computed.personal_model
            open_health_bundle = computed.open_health_insights
            morning_briefing = computed.morning_briefing
            response_profile = TrainingResponseProfile(
                analysis_run_id=run.id,
                user_id=user_id,
                date=target,
                generated_at=raw.as_of,
                responses=training_responses,
            )
            with self._uow_factory() as uow:
                repo = uow.repository
                if not repo.lock_analysis_input_revision(
                    user_id, run.input_revision_used
                ):
                    raise RuntimeError("分析输入在计算期间发生变化，请重新运行")
                current_profile = repo.user_profile(user_id)
                current_digest = analysis_policy_digest(
                    self._timezone,
                    run.intelligence_version,
                    run.decision_policy_version,
                    run.evidence_version,
                    self._catalog_revision,
                    installed_analysis_rules_digest(),
                )
                if (
                    current_profile.revision != run.profile_revision_used
                    or current_digest != run.config_digest
                ):
                    raise RuntimeError("分析输入在计算期间发生变化，请重新运行")
                if job_claim is not None:
                    if not job_repository.succeed(uow.transaction, job_claim, run.id):
                        raise RuntimeError("analysis job claim is no longer current")
                persisted_events = EventLifecycleEngine().reconcile(
                    repo,
                    run.id,
                    user_id,
                    target,
                    list(trace.detected_events),
                )
                if persisted_events != daily.events:
                    daily = daily.model_copy(update={"events": persisted_events})
                    weekly, monthly, morning_briefing = build_report_projections(
                        run.id,
                        trace.prepared_raw,
                        daily,
                        association_profile.associations,
                        weekly_feedback,
                        monthly_feedback,
                        tuple(EVIDENCE_REFS),
                        open_health_bundle,
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
                repo.rearm_unavailable_notification_deliveries(user_id, run.id, target)
                if (
                    job_claim is not None
                    and job_claim.delivery_period in {"morning", "evening"}
                ):
                    repo.enqueue_notification_delivery(
                        user_id,
                        run.id,
                        job_claim.delivery_period,
                        target,
                    )
                completed_run = _run_from_row(row)
                uow.commit()
            return AnalysisResult(
                run=completed_run,
                daily=daily,
                weekly=weekly,
                monthly=monthly,
                recommendation=recommendation,
                training_responses=training_responses,
                personal_model=personal_model,
                personal_associations=association_profile,
                open_health_insights=open_health_bundle,
                morning_briefing=morning_briefing,
            )
        except Exception as exc:
            with self._uow_factory() as uow:
                repo = uow.repository
                prior_run = repo.analysis_run(user_id, run.id)
                if prior_run is not None and prior_run.status == AnalysisRunStatus.RUNNING.value:
                    safe_error = (
                        "analysis_input_changed"
                        if isinstance(exc, RuntimeError)
                        and "分析输入在计算期间发生变化" in str(exc)
                        else "analysis_failed"
                    )
                    repo.complete_analysis_run(
                        run.id, AnalysisRunStatus.FAILED.value, safe_error
                    )
                    uow.commit()
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
        """Pure daily builder retained as the existing fencing test seam."""
        return build_daily_profile(analysis_run_id, raw, identity)


class IntelligenceQuery:
    """Read already persisted intelligence without triggering analysis."""

    def __init__(
        self,
        uow_factory: IntelligenceUowFactory,
        *,
        today_factory: Callable[[], date],
    ) -> None:
        self._uow_factory = uow_factory
        self._today_factory = today_factory

    def profile(self, user_id: str) -> UserProfile:
        with self._uow_factory() as uow:
            return uow.repository.user_profile(user_id)

    def daily(self, user_id: str, day: date | None = None) -> DailyProfile | None:
        target = day or self._today_factory()
        with self._uow_factory() as uow:
            row = uow.repository.latest_analysis_snapshot(user_id, "daily", target)
            return DailyProfile.model_validate(row.payload) if row else None

    def morning_briefing(
        self, user_id: str, day: date | None = None
    ) -> MorningBriefing | None:
        daily = self.daily(user_id, day)
        return MorningBriefingEngine().build(daily) if daily is not None else None

    def evening_briefing(
        self, user_id: str, day: date | None = None
    ) -> ReportBriefing | None:
        from vitalis.intelligence.evening_briefing import EveningBriefingEngine

        daily = self.daily(user_id, day)
        return EveningBriefingEngine().build(daily) if daily is not None else None

    def weekly_briefing(
        self, user_id: str, day: date | None = None
    ) -> ReportBriefing | None:
        from vitalis.intelligence.weekly_briefing import WeeklyBriefingEngine

        weekly = self.weekly(user_id, day)
        return WeeklyBriefingEngine().build(weekly) if weekly is not None else None

    def monthly_briefing(
        self, user_id: str, day: date | None = None
    ) -> ReportBriefing | None:
        from vitalis.intelligence.monthly_briefing import MonthlyBriefingEngine

        monthly = self.monthly(user_id, day)
        return MonthlyBriefingEngine().build(monthly) if monthly is not None else None

    def weekly(self, user_id: str, day: date | None = None) -> WeeklyProfile | None:
        target = day or self._today_factory()
        with self._uow_factory() as uow:
            row = uow.repository.latest_analysis_snapshot(user_id, "weekly", target)
            return WeeklyProfile.model_validate(row.payload) if row else None

    def monthly(self, user_id: str, day: date | None = None) -> MonthlyProfile | None:
        target = day or self._today_factory()
        with self._uow_factory() as uow:
            row = uow.repository.latest_analysis_snapshot(user_id, "monthly", target)
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
        with self._uow_factory() as uow:
            events = uow.repository.health_events(user_id, start, end, event_type)
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
        return DecisionExplanation(
            schema_version=DECISION_EXPLANATION_SCHEMA_VERSION,
            user_id=user_id,
            date=profile.date,
            snapshot=ExplanationSnapshotProvenance(
                analysis_run_id=profile.analysis_run_id,
                generated_at=profile.generated_at,
                schema_version=profile.schema_version,
                intelligence_version=profile.intelligence_version,
                decision_policy_version=profile.decision_policy_version,
                evidence_version=profile.evidence_version,
                data_quality=profile.data_quality,
            ),
            facts=profile.decision.evidence.facts,
            gates=profile.decision.evidence.gates,
            inferences=profile.decision.driver_labels,
            limitations=profile.decision.limitation_labels,
            action=profile.decision,
            evidence_refs=profile.evidence_refs,
        )

    def context(self, user_id: str, day: date | None = None) -> AgentContext | None:
        target = day or self._today_factory()
        with self._uow_factory() as uow:
            repo = uow.repository
            daily_row = repo.latest_analysis_snapshot(user_id, "daily", target)
            if daily_row is None:
                return None
            weekly_row = repo.analysis_snapshot_for_run(
                user_id, "weekly", target, daily_row.analysis_run_id
            )
            model_row = repo.analysis_snapshot_for_run(
                user_id, "personal_model", target, daily_row.analysis_run_id
            )
            if weekly_row is None or model_row is None:
                return None
            daily = DailyProfile.model_validate(daily_row.payload)
            weekly = WeeklyProfile.model_validate(weekly_row.payload)
            personal_model = PersonalModel.model_validate(model_row.payload)
            feedback = repo.subjective_feedback(user_id, target - timedelta(days=6), target)
            events = repo.active_health_events(user_id)
            profile = repo.user_profile(user_id)
        return AgentContextEngine().build(
            daily, weekly, events, feedback, personal_model, profile
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
        with self._uow_factory() as uow:
            repo = uow.repository
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
        with self._uow_factory() as uow:
            return uow.repository.subjective_feedback(user_id, start, end)

    def training_preferences(self, user_id: str) -> TrainingPreferences:
        with self._uow_factory() as uow:
            return uow.repository.training_preferences(user_id)

    def recommendation(
        self, user_id: str, recommendation_id: str
    ) -> RecommendationInstance | None:
        with self._uow_factory() as uow:
            return uow.repository.recommendation(user_id, recommendation_id)

    def training_responses(
        self, user_id: str, day: date | None = None
    ) -> TrainingResponseProfile | None:
        target = day or self._today_factory()
        with self._uow_factory() as uow:
            row = uow.repository.latest_analysis_snapshot(
                user_id, "training_responses", target
            )
            return TrainingResponseProfile.model_validate(row.payload) if row else None

    def personal_model(
        self, user_id: str, day: date | None = None
    ) -> PersonalModel | None:
        target = day or self._today_factory()
        with self._uow_factory() as uow:
            row = uow.repository.latest_analysis_snapshot(
                user_id, "personal_model", target
            )
            return PersonalModel.model_validate(row.payload) if row else None

    def personal_associations(
        self, user_id: str, day: date | None = None
    ) -> PersonalAssociationProfile | None:
        target = day or self._today_factory()
        with self._uow_factory() as uow:
            row = uow.repository.latest_analysis_snapshot(
                user_id, "personal_associations", target
            )
            return PersonalAssociationProfile.model_validate(row.payload) if row else None

    def deliveries(self, user_id: str, limit: int = 100) -> list[dict]:
        """Return user-scoped delivery state without lease or provider errors."""
        with self._uow_factory() as uow:
            rows = uow.repository.notification_deliveries(user_id, limit=limit)
        return [
            {
                "delivery_id": row.id,
                "analysis_run_id": row.analysis_run_id,
                "target_date": row.target_date,
                "period": row.period,
                "status": row.status,
                "attempt_count": row.attempt_count,
                "next_attempt_at": row.next_attempt_at,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
            for row in rows
        ]


class IntelligenceAction:
    """Persist explicit user actions without running health analysis."""

    def __init__(
        self,
        uow_factory: IntelligenceUowFactory,
        *,
        today_factory: Callable[[], date],
    ) -> None:
        self._uow_factory = uow_factory
        self._today_factory = today_factory

    def patch_profile(
        self, user_id: str, profile_patch: UserProfilePatch
    ) -> UserProfile:
        with self._uow_factory() as uow:
            repo = uow.repository
            repo.upsert_user(user_id)
            result = repo.patch_user_profile(user_id, profile_patch)
            uow.commit()
            return result

    def acknowledge_event(self, user_id: str, event_id: str) -> HealthEvent | None:
        with self._uow_factory() as uow:
            result = uow.repository.acknowledge_health_event(user_id, event_id)
            uow.commit()
            return result

    def complete_recommendation(
        self,
        user_id: str,
        recommendation_id: str,
        workout_id: str,
        workout_source: str = "zepp",
    ) -> RecommendationInstance:
        with self._uow_factory() as uow:
            result = uow.repository.link_recommendation(
                user_id, recommendation_id, workout_id, workout_source
            )
            uow.commit()
            return result

    def log_feedback(
        self,
        user_id: str,
        feedback_input: SubjectiveFeedbackInput,
        *,
        idempotency_key: str | None = None,
    ) -> SubjectiveFeedback:
        """Validate and persist feedback, including any idempotency ledger write."""
        request_hash = (
            _feedback_request_hash(feedback_input)
            if idempotency_key is not None
            else None
        )
        with self._uow_factory() as uow:
            repo = uow.repository
            if idempotency_key is None:
                result = self._save_feedback(repo, user_id, feedback_input)
            else:
                result = repo.create_or_reuse_feedback_request(
                    user_id,
                    idempotency_key,
                    request_hash,
                    lambda: self._save_feedback(repo, user_id, feedback_input),
                )
            uow.commit()
            return result

    def _save_feedback(
        self,
        repository: object,
        user_id: str,
        feedback_input: SubjectiveFeedbackInput,
    ) -> SubjectiveFeedback:
        target = feedback_input.date or self._today_factory()
        if feedback_input.workout_id and not repository.workout(
            user_id,
            feedback_input.workout_id,
            source=feedback_input.workout_source or "",
        ):
            raise ValueError("指定训练不存在或不属于当前用户")
        if feedback_input.recommendation_id:
            recommendation = repository.recommendation(
                user_id, feedback_input.recommendation_id
            )
            if recommendation is None:
                raise ValueError("训练建议不存在或不属于当前用户")
            if (
                recommendation.linked_workout_source != feedback_input.workout_source
                or recommendation.linked_workout_id != feedback_input.workout_id
            ):
                raise ValueError("训练反馈与建议关联的训练不一致")
        feedback = SubjectiveFeedback(
            id=uuid4().hex,
            user_id=user_id,
            date=target,
            **feedback_input.model_dump(exclude={"date"}),
        )
        return repository.save_subjective_feedback(feedback)

    def confirm_strength_workout(
        self,
        user_id: str,
        workout_id: str,
        confirmation: StrengthWorkoutConfirmationInput,
        workout_source: str = "zepp",
    ) -> list[StrengthExerciseRecord]:
        with self._uow_factory() as uow:
            repo = uow.repository
            workout = repo.workout(user_id, workout_id, source=workout_source)
            if workout is None:
                raise ValueError("指定训练不存在或不属于当前用户")
            data = workout.data or {}
            if data.get("training_family") != "strength" and data.get("type") != "strength":
                raise ValueError("指定训练不是力量训练")
            exercises = [
                normalize_exercise(
                    user_id,
                    workout_id,
                    order,
                    item,
                    confirmation.session_focus,
                    workout_source=workout_source,
                )
                for order, item in enumerate(confirmation.exercises, start=1)
            ]
            result = repo.replace_strength_exercises(
                user_id, workout_id, exercises, workout_source
            )
            uow.commit()
            return result

    def set_training_preferences(
        self, user_id: str, preferences: TrainingPreferenceInput
    ) -> TrainingPreferences:
        with self._uow_factory() as uow:
            repo = uow.repository
            repo.upsert_user(user_id)
            result = repo.save_training_preferences(user_id, preferences)
            uow.commit()
            return result

    def patch_training_preferences(
        self, user_id: str, patch: TrainingPreferencePatch
    ) -> TrainingPreferences:
        with self._uow_factory() as uow:
            repo = uow.repository
            repo.upsert_user(user_id)
            result = repo.patch_training_preferences(user_id, patch)
            uow.commit()
            return result


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
        profile_revision_used=row.profile_revision_used,
        input_revision_used=row.input_revision_used,
        config_digest=row.config_digest,
    )
