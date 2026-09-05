"""Deterministic evening projection from one already-computed DailyProfile."""

from typing import Any


class EveningBriefingEngine:
    """Extract today's recorded facts without inferring recovery or tomorrow."""

    def build_payload(self, daily: Any) -> dict:
        payload = (
            daily.model_dump(mode="json")
            if hasattr(daily, "model_dump")
            else dict(daily)
        )
        payload["report_context"] = dict(payload.get("report_context") or {})
        report_date = payload.get("date")
        training = (payload.get("features") or {}).get("training") or {}
        payload["evening_facts"] = {
            "today_workouts": [
                item for item in training.get("recent_workouts", [])
                if item.get("date") == report_date
            ],
            "recovery_state_label": (
                ((payload.get("features") or {}).get("recovery") or {}).get("state_label")
            ),
            "recorded_date": report_date,
        }
        payload["briefing_period"] = "evening"
        return payload

    def build(self, daily: Any) -> dict:
        return self.build_payload(daily)
