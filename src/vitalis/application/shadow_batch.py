"""Off-peak batch for shadow-only analysis.

The daily report path reads compact daily features and skips every shadow
module.  This batch runs once a night for the previous local day, reads the
full inputs the shadow modules need, and stores each result in its own row.
Nothing here is rendered; promotion of any module needs a comparison shown to
and confirmed by the user (see ``docs/reports.md``).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Callable

from vitalis.application.feedback_links import plan_completed
from vitalis.application.ports import IntelligenceUowFactory
from vitalis.intelligence.contracts import OpenHealthBundle
from vitalis.intelligence.open_health import OpenHealthEngine
from vitalis.intelligence.profile import ProfileLoader, RawDailyProfile
from vitalis.intelligence.response_model import evaluate, rows_from_series
from vitalis.intelligence.readiness import (
    body_signal_warning,
    latest_value,
    readiness_score,
)

log = logging.getLogger("vitalis.shadow")

OPEN_HEALTH_VERSION = "open-health-1.0"
RESPONSE_MODEL_VERSION = "response-model-1"
TRACKING_VERSION = "tracking-1"
# Full samples for the 28 nights the open-health inputs attribute to sleep days.
SHADOW_DETAIL_DAYS = 28


def build_open_health_bundle(raw: RawDailyProfile, timezone_name: str) -> OpenHealthBundle | None:
    """EWMA readiness, multivariate anomaly, sleep and TRIMP load (shadow only)."""
    if raw.open_health_input_failed:
        return None
    engine = OpenHealthEngine()
    bundle = engine.analyze(
        raw.open_health_observations, raw.user_profile, target_date=raw.day,
    )
    load = engine.analyze_load(
        raw.open_health_load_workouts,
        raw.user_profile,
        target_date=raw.day,
        period_start=raw.day - timedelta(days=41),
        queried_days=raw.open_health_load_queried_days,
        rhr_by_day=raw.open_health_rhr_by_day,
        upstream_coverage_verified=raw.open_health_load_upstream_coverage_verified,
        input_truncated=raw.open_health_load_truncated,
        timezone_name=timezone_name,
    )
    return bundle.model_copy(update={"training_load": load})


class ShadowBatch:
    def __init__(
        self,
        uow_factory: IntelligenceUowFactory,
        *,
        timezone_name: str,
        now_factory: Callable[[], datetime],
    ) -> None:
        self._uow_factory = uow_factory
        self._timezone = timezone_name
        self._now_factory = now_factory

    @staticmethod
    def _response_model(raw: RawDailyProfile, feedback: list[dict]) -> dict:
        rows = rows_from_series(
            raw.series, feedback, raw.day - timedelta(days=179), raw.day,
            verified_training_days=_verified_days(raw),
        )
        return {"version": RESPONSE_MODEL_VERSION, **evaluate(rows)}

    def run(self, user_id: str, day: date) -> dict[str, str]:
        """Compute and store every shadow result for one closed local day."""
        as_of = self._now_factory()
        with self._uow_factory() as uow:
            repo = uow.repository
            raw = ProfileLoader(
                repo, detail_days=SHADOW_DETAIL_DAYS, shadow_inputs=True,
            ).load(user_id, day, as_of=as_of, timezone_name=self._timezone)
            daily_row = repo.latest_analysis_snapshot(user_id, "daily", day)
            feedback = [
                item.model_dump(mode="json")
                for item in repo.subjective_feedback(user_id, day - timedelta(days=179), day)
            ]
            daily_payloads = [
                row.payload for row in (
                    repo.latest_analysis_snapshot(user_id, "daily", day - timedelta(days=offset))
                    for offset in range(7)
                ) if row is not None
            ]
            warnings = [
                (row.day, row.payload) for row in repo.shadow_results(
                    user_id, "body_signal", day - timedelta(days=27), day,
                ) if row.payload.get("warning")
            ]
            recovery = (
                ((daily_row.payload.get("features") or {}).get("recovery") or {})
                if daily_row is not None else {}
            )
        results: dict[str, tuple[str, dict]] = {}
        try:
            bundle = build_open_health_bundle(raw, self._timezone)
        except Exception:
            log.exception("open health shadow failed: user=%s day=%s", user_id, day)
            bundle = None
        if bundle is not None:
            results["open_health"] = (OPEN_HEALTH_VERSION, bundle.model_dump(mode="json"))
        readiness = readiness_score(raw)
        readiness["comparison"] = {
            "vote_state": recovery.get("state"),
            "vendor_readiness": latest_value(raw.series.get("readiness", []), day),
            "vendor_charge": latest_value(raw.series.get("hybrid_charge", []), day),
        }
        results["readiness"] = (readiness["version"], readiness)
        body = body_signal_warning(raw)
        body.pop("event", None)
        results["body_signal"] = (body["version"], body)
        results["response_model"] = (RESPONSE_MODEL_VERSION, self._response_model(raw, feedback))
        results["tracking"] = (TRACKING_VERSION, tracking_metrics(
            results["response_model"][1], daily_payloads, warnings, feedback,
        ))
        with self._uow_factory() as uow:
            for kind, (version, payload) in results.items():
                uow.repository.save_shadow_result(user_id, day, kind, version, payload)
            uow.commit()
        log.info("shadow batch stored: user=%s day=%s kinds=%s", user_id, day, ",".join(results))
        return {kind: version for kind, (version, _) in results.items()}


def tracking_metrics(
    response_model: dict, daily_payloads: list[dict], warnings: list[tuple[date, dict]],
    feedback: list[dict],
) -> dict:
    """The four weekly quality measures of the system itself.

    HRV prediction error (model against naive), body-signal warnings the user
    did not confirm within two days (illness tag or feeling 1–2), the share of
    planned sessions the training record shows as done, and the share of
    feedback marked useful.  Unknown measures stay ``None``.
    """
    hrv = (response_model.get("targets") or {}).get("hrv") or {}
    confirmations = {
        date.fromisoformat(str(item.get("date"))[:10])
        for item in feedback
        if item.get("date") and (
            "illness" in (item.get("behavior_tags") or [])
            or (item.get("feeling_score") is not None and item["feeling_score"] <= 2)
        )
    }
    unconfirmed = [
        day for day, _ in warnings
        if not any(day + timedelta(days=offset) in confirmations for offset in range(3))
    ]
    executions = [value for value in (plan_completed(item) for item in daily_payloads) if value is not None]
    summaries = [(item.get("report_context") or {}).get("product_summary") or {} for item in daily_payloads]
    rated = sum(int(item.get("feedback_count") or 0) for item in summaries[:1])
    useful = sum(int(item.get("useful_count") or 0) for item in summaries[:1])
    return {
        "hrv_model_mae": hrv.get("model_mae"),
        "hrv_naive_mae": hrv.get("naive_mae"),
        "warnings_28d": len(warnings),
        "warnings_unconfirmed_28d": len(unconfirmed),
        "warning_unconfirmed_rate": round(len(unconfirmed) / len(warnings), 3) if warnings else None,
        "plan_execution_rate_7d": round(sum(executions) / len(executions), 3) if executions else None,
        "feedback_useful_rate": round(useful / rated, 3) if rated else None,
    }


def _verified_days(raw: RawDailyProfile) -> set[date]:
    return {
        value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
        for value in (raw.training_history_coverage or {}).get("verified_days", [])
        if isinstance(value, (date, str))
    }


def _band(score: float | None) -> str | None:
    if score is None:
        return None
    return "GOOD" if score >= 67 else "NORMAL" if score >= 34 else "SUPPRESSED"


def _rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        for index in order[position:end + 1]:
            ranks[index] = (position + end) / 2 + 1
        position = end + 1
    return ranks


def _spearman(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 5:
        return None
    left, right = _rank([a for a, _ in pairs]), _rank([b for _, b in pairs])
    mean_left, mean_right = sum(left) / len(left), sum(right) / len(right)
    numerator = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right))
    denominator = (
        sum((a - mean_left) ** 2 for a in left) * sum((b - mean_right) ** 2 for b in right)
    ) ** 0.5
    return round(numerator / denominator, 3) if denominator else None


def shadow_report(kind: str, rows: list[tuple[date, dict]]) -> dict:
    """Summarize a shadow period for the user's promotion decision."""
    if kind == "readiness":
        days = []
        for day, payload in rows:
            comparison = payload.get("comparison") or {}
            days.append({
                "date": day.isoformat(),
                "score": payload.get("score"),
                "band": _band(payload.get("score")),
                "hrv_state": payload.get("hrv_state"),
                "factors": payload.get("factors") or [],
                "vote_state": comparison.get("vote_state"),
                "vendor_readiness": comparison.get("vendor_readiness"),
            })
        scored = [item for item in days if item["score"] is not None]
        voted = [item for item in scored if item["vote_state"] in {"GOOD", "NORMAL", "SUPPRESSED"}]
        return {
            "kind": kind,
            "days": len(days),
            "scored_days": len(scored),
            "agreement_with_vote": (
                round(sum(item["band"] == item["vote_state"] for item in voted) / len(voted), 3)
                if voted else None
            ),
            "spearman_with_vendor": _spearman([
                (item["score"], item["vendor_readiness"]) for item in scored
                if item["vendor_readiness"] is not None
            ]),
            "daily": days,
        }
    if kind == "body_signal":
        evaluated = [
            (day, payload) for day, payload in rows
            if sum(item.get("status") == "AVAILABLE" for item in (payload.get("inputs") or {}).values()) >= 2
        ]
        warnings = [(day, payload) for day, payload in evaluated if payload.get("warning")]
        return {
            "kind": kind,
            "days": len(rows),
            "evaluated_days": len(evaluated),
            "warning_days": [
                {"date": day.isoformat(), "evidence": payload.get("evidence") or []}
                for day, payload in warnings
            ],
            "warning_rate": round(len(warnings) / len(evaluated), 3) if evaluated else None,
            "note": "每个预警日需要用户确认当时是否确实疲劳或不适，才能算出误报率。",
        }
    raise ValueError(f"unknown shadow kind: {kind}")
