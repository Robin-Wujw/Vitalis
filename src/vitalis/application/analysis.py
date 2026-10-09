"""Pure analysis entry point and explicit analysis value objects.

The application orchestrator prepares ``AnalysisDataset`` from durable state and
publishes the returned result.  This module never opens a database, performs
network I/O, reads process settings, or obtains the current time.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Mapping
from zoneinfo import ZoneInfo

from vitalis.application.report_context import attach_report_context
from vitalis.intelligence.analyzers import (
    HrvAnalyzer,
    OvernightVitalsAnalyzer,
    RecoveryAnalyzer,
    SleepAnalyzer,
    TrainingAnalyzer,
    build_states,
)
from vitalis.intelligence.association import PersonalAssociationEngine
from vitalis.intelligence.baseline import BaselineEngine
from vitalis.intelligence.contracts import (
    AnalysisResult,
    AnalysisRun,
    AnalysisRunStatus,
    DailyProfile,
    EvidenceRef,
    HealthEvent,
    MonthlyProfile,
    MorningBriefing,
    OpenHealthBundle,
    PersonalAssociation,
    RecommendationInstance,
    SubjectiveFeedback,
    WeeklyProfile,
    INTELLIGENCE_VERSION,
    DECISION_POLICY_VERSION,
    EVIDENCE_VERSION,
)
from vitalis.intelligence.decision import DecisionEngine
from vitalis.intelligence.events import HealthEventEngine
from vitalis.intelligence.lifecycle import EventLifecycleEngine
from vitalis.intelligence.monthly import MonthlyProfileEngine
from vitalis.intelligence.morning_briefing import MorningBriefingEngine
from vitalis.intelligence.open_health import OpenHealthEngine
from vitalis.intelligence.personal import PersonalModelEngine
from vitalis.intelligence.facts import facts_for_day
from vitalis.intelligence.profile import RawDailyProfile
from vitalis.intelligence.training_response import TrainingResponseEngine
from vitalis.intelligence.weekly import WeeklyProfileEngine
from vitalis.intelligence.report_periods import resolve_month_period, resolve_week_period


@dataclass(frozen=True)
class AnalysisRequest:
    """All request-scoped values needed to make one analysis reproducible."""

    user_id: str
    target_date: date
    analysis_run_id: str
    as_of: datetime
    timezone: str
    profile_revision: int = 0
    input_revision: int = 0
    config_digest: str = ""
    completed_at: datetime | None = None

    @property
    def run_id(self) -> str:
        """Convenient alias for callers that use the persistence name."""
        return self.analysis_run_id


@dataclass(frozen=True)
class AnalysisPolicy:
    """Versioned, explicit policy inputs; no process configuration is consulted."""

    timezone: str
    evidence_refs: tuple[EvidenceRef, ...] = ()
    intelligence_version: str = INTELLIGENCE_VERSION
    decision_policy_version: str = DECISION_POLICY_VERSION
    evidence_version: str = EVIDENCE_VERSION
    open_health_enabled: bool = True


@dataclass(frozen=True)
class AnalysisDataset:
    """Prepared immutable-by-convention inputs supplied by the application layer."""

    raw: RawDailyProfile
    identity: Mapping[str, object] = field(default_factory=dict)
    response_feedback: tuple[SubjectiveFeedback, ...] = ()
    weekly_feedback: tuple[dict, ...] | None = None
    monthly_feedback: tuple[dict, ...] | None = None
    recommendation_by_workout: Mapping[tuple[str, str] | str, str] = field(
        default_factory=dict
    )
    prior_events: tuple[HealthEvent, ...] = ()
    product_contexts: Mapping[str, dict] = field(default_factory=dict)


@dataclass(frozen=True)
class AnalysisTrace:
    """Pure result and prepared inputs needed by the publication transaction."""

    result: AnalysisResult
    detected_events: tuple[HealthEvent, ...]
    prepared_raw: RawDailyProfile


DailyBuilder = Callable[[str, RawDailyProfile, Mapping[str, object]], DailyProfile]


def analyze(
    dataset: AnalysisDataset,
    request: AnalysisRequest,
    policy: AnalysisPolicy,
) -> AnalysisResult:
    """Compute all report projections from explicit inputs only."""

    return analyze_with_trace(dataset, request, policy).result


def analyze_with_trace(
    dataset: AnalysisDataset,
    request: AnalysisRequest,
    policy: AnalysisPolicy,
    *,
    daily_builder: DailyBuilder | None = None,
) -> AnalysisTrace:
    """Compute a result and expose detected events for transactional persistence.

    ``daily_builder`` is an injectable pure function used by the application
    orchestrator to retain its existing fencing test seam.  It is never a
    repository callback and has no side effects in the default path.
    """

    _validate_inputs(dataset, request, policy)
    raw = _raw_for_request(dataset.raw, request)
    identity = dict(dataset.identity)
    build_daily = daily_builder or build_daily_profile

    daily = build_daily(request.analysis_run_id, raw, identity)
    daily = daily.model_copy(update={"evidence_refs": list(policy.evidence_refs)})
    detected_events = tuple(daily.events)
    daily = daily.model_copy(update={
        "events": EventLifecycleEngine.reconcile_state(
            list(dataset.prior_events),
            list(detected_events),
            request.target_date,
        ),
    })

    open_health_bundle = None
    open_health_failed = raw.open_health_input_failed
    if policy.open_health_enabled and not raw.open_health_input_failed:
        try:
            open_health_engine = OpenHealthEngine()
            open_health_bundle = open_health_engine.analyze(
                raw.open_health_observations,
                raw.user_profile,
                target_date=request.target_date,
            )
            open_health_load = open_health_engine.analyze_load(
                raw.open_health_load_workouts,
                raw.user_profile,
                target_date=request.target_date,
                period_start=request.target_date - timedelta(days=41),
                queried_days=raw.open_health_load_queried_days,
                rhr_by_day=raw.open_health_rhr_by_day,
                upstream_coverage_verified=raw.open_health_load_upstream_coverage_verified,
                input_truncated=raw.open_health_load_truncated,
                timezone_name=policy.timezone,
            )
            open_health_bundle = open_health_bundle.model_copy(
                update={"training_load": open_health_load}
            )
        except Exception:
            # Shadow-only calculations never change the core report or raise.
            open_health_failed = True
    if open_health_bundle is not None:
        daily = daily.model_copy(update={"open_health_insights": open_health_bundle})
    elif open_health_failed:
        metadata = dict(daily.metadata)
        metadata["open_health_status"] = "REFUSED_INTERNAL_ERROR"
        daily = daily.model_copy(update={"metadata": metadata})

    response_feedback = list(dataset.response_feedback)
    training_responses = TrainingResponseEngine().build(
        request.analysis_run_id,
        raw,
        response_feedback,
        dict(dataset.recommendation_by_workout),
    )
    association_profile = PersonalAssociationEngine().build(
        request.analysis_run_id,
        raw,
        generated_at=raw.as_of,
    )
    personal_model = PersonalModelEngine().build(
        request.analysis_run_id,
        daily,
        training_responses,
        association_profile.associations,
        feedback=response_feedback,
        generated_at=raw.as_of,
    )
    daily = attach_report_context(
        daily, raw, responses=training_responses, feedback=response_feedback,
        associations=association_profile.associations,
        product_context=dataset.product_contexts.get("daily", {}),
    )
    recommendation = RecommendationInstance(
        id=daily.decision.recommendation_id,
        analysis_run_id=request.analysis_run_id,
        user_id=request.user_id,
        date=request.target_date,
        decision=daily.decision,
        created_at=raw.as_of,
    )

    weekly_period = resolve_week_period(request.target_date)
    monthly_period = resolve_month_period(request.target_date)
    weekly_feedback = _feedback_for_period(
        dataset.weekly_feedback
        if dataset.weekly_feedback is not None
        else [item.model_dump(mode="json") for item in response_feedback],
        weekly_period.start,
        weekly_period.end,
    )
    monthly_feedback = _feedback_for_period(
        dataset.monthly_feedback
        if dataset.monthly_feedback is not None
        else [item.model_dump(mode="json") for item in response_feedback],
        monthly_period.start,
        monthly_period.end,
    )
    weekly, monthly, morning_briefing = build_report_projections(
        request.analysis_run_id,
        raw,
        daily,
        association_profile.associations,
        weekly_feedback,
        monthly_feedback,
        policy.evidence_refs,
        open_health_bundle,
        training_responses=training_responses,
        response_feedback=response_feedback,
        product_contexts=dataset.product_contexts,
    )
    run = AnalysisRun(
        id=request.analysis_run_id,
        user_id=request.user_id,
        target_date=request.target_date,
        status=AnalysisRunStatus.SUCCEEDED,
        started_at=request.as_of,
        completed_at=request.completed_at,
        intelligence_version=policy.intelligence_version,
        decision_policy_version=policy.decision_policy_version,
        evidence_version=policy.evidence_version,
        profile_revision_used=request.profile_revision,
        input_revision_used=request.input_revision,
        config_digest=request.config_digest,
    )
    return AnalysisTrace(
        result=AnalysisResult(
            run=run,
            daily=daily,
            weekly=weekly,
            monthly=monthly,
            recommendation=recommendation,
            training_responses=training_responses,
            personal_model=personal_model,
            personal_associations=association_profile,
            open_health_insights=open_health_bundle,
            morning_briefing=morning_briefing,
        ),
        detected_events=detected_events,
        prepared_raw=raw,
    )


def build_report_projections(
    analysis_run_id: str,
    raw: RawDailyProfile,
    daily: DailyProfile,
    associations: list[PersonalAssociation],
    weekly_feedback: list[dict],
    monthly_feedback: list[dict],
    evidence_refs: tuple[EvidenceRef, ...],
    open_health_insights: OpenHealthBundle | None,
    *,
    training_responses: list | None = None,
    response_feedback: list[SubjectiveFeedback] | None = None,
    product_contexts: Mapping[str, dict] | None = None,
) -> tuple[WeeklyProfile, MonthlyProfile, MorningBriefing]:
    """Build every report view that depends on the finalized daily events."""
    responses = training_responses or []
    feedback = response_feedback or []
    products = product_contexts or {}
    period_associations = {}
    for kind, period in (("weekly", resolve_week_period(raw.day)), ("monthly", resolve_month_period(raw.day))):
        period_raw = replace(raw, day=period.end)
        period_associations[kind] = PersonalAssociationEngine().build(
            analysis_run_id, period_raw, generated_at=raw.as_of,
        ).associations
    weekly = WeeklyProfileEngine().build(
        analysis_run_id,
        raw,
        daily.trends,
        daily.events,
        feedback=weekly_feedback,
        evidence_refs=list(evidence_refs),
        open_health_insights=open_health_insights,
        generated_at=raw.as_of,
        period_mode="calendar",
    )
    weekly = attach_report_context(
        weekly, raw, responses=[item for item in responses if weekly.period_start <= item.exposure.date <= weekly.period_end],
        feedback=[item for item in feedback if weekly.period_start <= item.date <= weekly.period_end],
        associations=period_associations["weekly"], product_context=products.get("weekly", products.get("daily", {})),
    )
    monthly = MonthlyProfileEngine().build(
        analysis_run_id,
        raw,
        daily.trends,
        daily.events,
        period_associations["monthly"],
        feedback=monthly_feedback,
        evidence_refs=list(evidence_refs),
        open_health_insights=open_health_insights,
        generated_at=raw.as_of,
    )
    monthly = attach_report_context(
        monthly, raw,
        responses=[item for item in responses if monthly.period_start <= item.exposure.date <= monthly.period_end],
        feedback=[item for item in feedback if monthly.period_start <= item.date <= monthly.period_end],
        associations=period_associations["monthly"],
        product_context=products.get("monthly", products.get("daily", {})),
    )
    return weekly, monthly, MorningBriefingEngine().build(daily)


def build_daily_profile(
    analysis_run_id: str,
    raw: RawDailyProfile,
    identity: Mapping[str, object],
) -> DailyProfile:
    """Build the daily projection without persistence or ambient inputs."""

    target = raw.day
    baselines = BaselineEngine().build(raw.series, target)
    from vitalis.intelligence.activity import ActivityAnalyzer

    activity = ActivityAnalyzer().analyze(raw, baselines)
    previous_day = target - timedelta(days=1)
    previous_raw = replace(
        raw,
        day=previous_day,
        report_context={"target_day_complete": True},
        sample_window_summaries={},
    )
    previous_raw.facts = facts_for_day(previous_raw)
    previous_activity = ActivityAnalyzer().analyze(previous_raw, {})
    report_context = dict(raw.report_context)
    if previous_activity.status.value == "AVAILABLE":
        report_context["previous_day_activity"] = {
            "user_id": raw.user_id,
            "date": previous_day.isoformat(),
            "as_of": raw.as_of.isoformat(),
            "activity": previous_activity.model_dump(mode="json"),
        }
    sleep, sleep_state = SleepAnalyzer().analyze(raw, baselines)
    hrv = HrvAnalyzer().analyze(raw, baselines)
    overnight_vitals = OvernightVitalsAnalyzer().analyze(raw, baselines, sleep)
    training = TrainingAnalyzer().analyze(raw, baselines)
    recovery = RecoveryAnalyzer().analyze(raw, sleep, sleep_state, hrv, training)
    recommendation_id = _stable_id("recommendation", analysis_run_id)
    decision = DecisionEngine().decide(
        recommendation_id,
        target,
        sleep,
        sleep_state,
        hrv,
        recovery,
        training,
        raw.training_preferences,
        timezone_name=raw.timezone_name,
    )
    from vitalis.intelligence.trend import TrendEngine

    trends = TrendEngine().calculate(raw)
    events = HealthEventEngine().detect(raw, baselines, trends, hrv, recovery)
    return DailyProfile(
        analysis_run_id=analysis_run_id,
        user_id=raw.user_id,
        date=target,
        generated_at=raw.as_of,
        data_quality=raw.data_quality,
        report_context=report_context,
        facts=raw.facts,
        baselines=baselines,
        features={
            "activity": activity,
            "sleep": sleep,
            "hrv": hrv,
            "overnight_vitals": overnight_vitals,
            "recovery": recovery,
            "training": training,
        },
        trends=trends,
        events=events,
        states=build_states(sleep_state, recovery, training),
        decision=decision,
        evidence_refs=[],
        metadata={
            "identity": identity,
            "profile_revision_used": raw.user_profile.revision,
            "user_profile": raw.user_profile.model_dump(mode="json"),
            "baseline_policy": {
                "windows_days": [7, 28],
                "minimum_distinct_days": {"7": 3, "28": 14},
                "policy_type": "product_policy_not_medical_threshold",
            },
            "diagnostic_use": False,
        },
    )


def _stable_id(prefix: str, value: str) -> str:
    from hashlib import sha256

    return f"{prefix}-{sha256(value.encode('utf-8')).hexdigest()[:32]}"


def _raw_for_request(raw: RawDailyProfile, request: AnalysisRequest) -> RawDailyProfile:
    as_of = request.as_of
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    else:
        as_of = as_of.astimezone(timezone.utc)
    if raw.user_id != request.user_id:
        raise ValueError("analysis dataset user does not match request")
    if raw.day != request.target_date:
        raise ValueError("analysis dataset day does not match request")
    report_context = dict(raw.report_context)
    report_context["timezone"] = request.timezone
    report_context["as_of"] = as_of.isoformat()
    return replace(
        raw,
        as_of=as_of,
        timezone_name=request.timezone,
        report_context=report_context,
    )


def _feedback_for_period(items: list[dict], start: date, end: date) -> list[dict]:
    output = []
    for item in items:
        value = item.get("date") if isinstance(item, dict) else getattr(item, "date", None)
        if isinstance(value, str):
            try:
                value = date.fromisoformat(value[:10])
            except ValueError:
                continue
        if isinstance(value, date) and start <= value <= end:
            output.append(item)
    return output


def _validate_inputs(
    dataset: AnalysisDataset,
    request: AnalysisRequest,
    policy: AnalysisPolicy,
) -> None:
    if not request.user_id or not request.analysis_run_id:
        raise ValueError("analysis request requires user and run identifiers")
    if not request.timezone or request.timezone != policy.timezone:
        raise ValueError("request and policy timezones must match")
    # Validate the zone now, while retaining the zone as an explicit input.
    ZoneInfo(policy.timezone)
    if request.as_of.tzinfo is None:
        raise ValueError("analysis request as_of must be timezone-aware")
    if request.profile_revision < 0 or request.input_revision < 0:
        raise ValueError("analysis revisions must be non-negative")
    if dataset.raw.user_id != request.user_id or dataset.raw.day != request.target_date:
        raise ValueError("analysis dataset does not match request")
