"""Typed health timeline projections without raw measurement samples."""

from datetime import date

from vitalis.storage import HealthRepository
from vitalis.time import local_day

from .contracts import HealthTimeline, TimelineItem, TrainingResponseProfile


EVENT_LIFECYCLE_LABELS = {
    "DETECTED": "已发现",
    "PERSISTING": "持续中",
    "IMPROVING": "改善中",
    "RESOLVED": "已恢复",
}


class HealthTimelineEngine:
    def build(
        self,
        repo: HealthRepository,
        user_id: str,
        start: date,
        end: date,
        response_profile: TrainingResponseProfile | None,
        limit: int = 100,
    ) -> HealthTimeline:
        items: list[TimelineItem] = []
        for run in repo.analysis_runs(user_id, start, end):
            items.append(TimelineItem(
                id=f"analysis:{run.id}",
                type="analysis",
                date=run.target_date,
                title="健康分析完成" if run.status == "SUCCEEDED" else "健康分析失败",
                summary=f"分析状态：{run.status}",
                references={"analysis_run_id": run.id},
                details={
                    "status": run.status,
                    "intelligence_version": run.intelligence_version,
                    "decision_policy_version": run.decision_policy_version,
                },
            ))
        for recommendation in repo.recommendations(user_id, start, end):
            items.append(TimelineItem(
                id=f"recommendation:{recommendation.id}",
                type="recommendation",
                date=recommendation.date,
                title=f"训练建议：{recommendation.decision.action_label}",
                summary="；".join(recommendation.decision.driver_labels[:3]) or "暂无主要判断依据",
                references={
                    "recommendation_id": recommendation.id,
                    "analysis_run_id": recommendation.analysis_run_id,
                    **({"workout_id": recommendation.linked_workout_id} if recommendation.linked_workout_id else {}),
                },
                details={
                    "completion_status": recommendation.completion_status.value,
                    "confidence_label": recommendation.decision.confidence_label,
                    "suggested_type_labels": recommendation.decision.suggested_type_labels[:3],
                },
            ))
        for workout in repo.workouts(user_id, start, end):
            data = workout.data or {}
            workout_day = local_day(workout.started_at) if workout.started_at else None
            if workout_day is None or not start <= workout_day <= end:
                continue
            items.append(TimelineItem(
                id=f"workout:{workout.workout_id}",
                type="workout",
                date=workout_day,
                title=f"完成训练：{data.get('sport_mode_label') or '未知运动'}",
                summary=f"{int(data.get('duration') or 0)} 分钟",
                references={"workout_id": workout.workout_id},
                details={
                    "sport_mode": data.get("sport_mode") or "unknown",
                    "training_family": data.get("training_family") or "skill",
                    "duration_minutes": int(data.get("duration") or 0),
                    "vendor_load": float(data.get("load") or 0),
                },
            ))
        for feedback in repo.subjective_feedback(user_id, start, end):
            details = {
                key: value for key, value in {
                    "session_rpe": feedback.session_rpe,
                    "physical_fatigue": feedback.physical_fatigue,
                    "mental_state": feedback.mental_state,
                    "muscle_soreness": feedback.muscle_soreness,
                }.items() if value is not None
            }
            items.append(TimelineItem(
                id=f"feedback:{feedback.id}",
                type="feedback",
                date=feedback.date,
                title="记录主观反馈",
                summary="已记录训练或当日主观感受",
                references={
                    "feedback_id": feedback.id,
                    **({"workout_id": feedback.workout_id} if feedback.workout_id else {}),
                    **({"recommendation_id": feedback.recommendation_id} if feedback.recommendation_id else {}),
                },
                details=details,
            ))
        for observation in repo.event_observations_range(user_id, start, end):
            event = repo.health_event(user_id, observation.event_id)
            if event is None:
                continue
            items.append(TimelineItem(
                id=f"event:{observation.id}",
                type="event_transition",
                date=observation.date,
                title=(
                    f"{event.type_label}："
                    f"{EVENT_LIFECYCLE_LABELS[observation.lifecycle.value]}"
                ),
                summary=event.summary,
                references={
                    "event_id": event.id,
                    "analysis_run_id": observation.analysis_run_id,
                },
                details={
                    "detected": observation.detected,
                    "previous_lifecycle": observation.previous_lifecycle.value if observation.previous_lifecycle else None,
                    "lifecycle": observation.lifecycle.value,
                },
            ))
        if response_profile:
            for response in response_profile.responses:
                if not start <= response.exposure.date <= end:
                    continue
                items.append(TimelineItem(
                    id=f"response:{response.analysis_run_id}:{response.exposure.workout_id}",
                    type="training_response",
                    date=response.exposure.date,
                    title=f"训练响应：{response.exposure.sport_mode_label}",
                    summary=response.recovery_status_label,
                    references={
                        "analysis_run_id": response.analysis_run_id,
                        "workout_id": response.exposure.workout_id,
                    },
                    details={
                        "recovery_status": response.recovery_status.value,
                        "recovery_hours": response.recovery_hours,
                        "confidence_label": response.confidence_label,
                        "overlap_count": len(response.overlapping_workout_ids),
                    },
                ))
        items.sort(key=lambda item: (item.date, item.type, item.id), reverse=True)
        return HealthTimeline(
            user_id=user_id,
            period_start=start,
            period_end=end,
            items=items[:limit],
        )
