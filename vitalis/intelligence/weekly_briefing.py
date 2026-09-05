"""Plain, deterministic projection of a WeeklyProfile for human delivery."""

from typing import Any


class WeeklyBriefingEngine:
    """Extract display sections while preserving the profile's source facts."""

    def build_payload(self, profile: Any) -> dict:
        payload = (
            profile.model_dump(mode="json")
            if hasattr(profile, "model_dump")
            else dict(profile)
        )
        context = dict(payload.get("report_context") or {})
        payload["report_context"] = context
        training = (payload.get("facts") or {}).get("training") or {}
        training_context = {
            key: training[key]
            for key in ("coverage_status", "record_days", "unknown_days", "totals_are_partial")
            if key in training
        }
        payload["weekly_sections"] = {
            "facts": payload.get("facts") or {},
            "trends": (payload.get("inferences") or {}).get("trends") or [],
            "key_changes": (payload.get("inferences") or {}).get("key_changes") or [],
            "limitations": (payload.get("inferences") or {}).get("limitations") or [],
            "recommendations": (payload.get("actions") or {}).get("recommendations") or [],
            "training_coverage": training_context,
        }
        return payload

    def build(self, profile: Any) -> dict:
        return self.build_payload(profile)
