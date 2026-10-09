"""Save qualified public facts and period summaries during analysis."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Mapping, Sequence

from vitalis.application.product_tracking import calculate_progress
from vitalis.intelligence.contracts import PersonalAssociation, SubjectiveFeedback, TrainingResponse
from vitalis.intelligence.personal import summarize_training_responses
from vitalis.intelligence.profile import RawDailyProfile
from vitalis.intelligence.signals import build_period_signals, build_signal_states


def attach_report_context(
    profile,
    raw: RawDailyProfile,
    *,
    responses: Sequence[TrainingResponse],
    feedback: Sequence[SubjectiveFeedback],
    associations: Sequence[PersonalAssociation],
    product_context: Mapping[str, object],
):
    period_start = getattr(profile, "period_start", raw.day)
    period_end = getattr(profile, "period_end", raw.day)
    is_daily = hasattr(profile, "date")
    if is_daily:
        signals = build_signal_states(raw, profile.baselines)
    else:
        period_raw = replace(raw, day=period_end, report_context=dict(profile.report_context))
        signals = build_period_signals(period_raw, period_start, period_end)
    selected_responses = [
        response for response in responses
        if period_start <= response.exposure.date <= period_end
    ]
    selected_feedback = [item for item in feedback if period_start <= item.date <= period_end]
    product = calculate_progress(
        {**dict(product_context), "user_id": raw.user_id, "day": period_end,
         "observed_as_of": raw.as_of},
        raw.series,
    ).as_dict()
    events = []
    for item in product_context.get("feedback", []):
        observed = item.get("occurred_on")
        if isinstance(observed, str):
            observed = date.fromisoformat(observed)
        if isinstance(observed, date) and period_start <= observed <= period_end:
            events.append(item)
    product.update({
        "feedback_count": len(events),
        "useful_count": sum(item.get("usefulness") == "useful" for item in events),
        "completed_count": sum(item.get("completed") is True for item in events),
        "correction_count": sum(item.get("kind") == "data_correction" for item in events),
        "period_start": period_start.isoformat(), "period_end": period_end.isoformat(),
        "as_of": raw.as_of.isoformat(),
    })
    summary = summarize_training_responses(
        selected_responses, selected_feedback, as_of=raw.as_of,
    )
    summary.update({"period_start": period_start.isoformat(), "period_end": period_end.isoformat()})
    trend_values = getattr(profile, "trends", None)
    if trend_values is None:
        trend_values = getattr(getattr(profile, "inferences", None), "trends", [])
    context = {
        **dict(profile.report_context),
        "signals": {key: [value.model_dump(mode="json") for value in values] for key, values in signals.items()},
        "long_term_trends": [item.model_dump(mode="json") for item in trend_values],
        "training_response_summary": summary,
        "personal_associations": [item.model_dump(mode="json") for item in associations],
        "product_summary": product,
    }
    return profile.model_copy(update={"report_context": context})
