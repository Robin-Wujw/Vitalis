"""The single request catalogue for synchronous and durable Zepp fetching."""

from dataclasses import dataclass


@dataclass(frozen=True)
class WellnessStream:
    label: str
    surface: str
    event_type: str
    sub_type: str | None
    health_stream: str
    limit: int = 1000
    raw_only: bool = False


WELLNESS_STREAMS = (
    WellnessStream("all_day_stress", "user", "all_day_stress", None, "wellness/all_day_stress"),
    WellnessStream("respiratory_rate", "v2", "RespiratoryRate", "real_data", "wellness/respiratory_rate"),
    WellnessStream("hrv_rmssd", "v2", "HRVRMSSD", "real_data", "wellness/hrv_rmssd"),
    WellnessStream("lactate_threshold", "v2", "LactateThreshold", "summary", "wellness/lactate_threshold"),
    WellnessStream("spo2", "user", "blood_oxygen", None, "wellness/spo2_point"),
    WellnessStream("pai", "user", "PaiHealthInfo", None, "wellness/pai"),
    WellnessStream("weight", "weight", "", None, "wellness/weight", limit=300),
    WellnessStream("exertion", "v2", "exertion", "algo_result", "wellness/exertion"),
    WellnessStream("charge_stress", "v2", "Charge", "stress_data", "wellness/charge_stress", raw_only=True),
    WellnessStream("charge_insights", "v2", "Charge", "insight_data", "wellness/charge_insights", raw_only=True),
    WellnessStream("readiness_raw", "v2", "readiness", "watch_score_data", "wellness/readiness_raw", raw_only=True),
)
RAW_ONLY_WELLNESS = frozenset(item.label for item in WELLNESS_STREAMS if item.raw_only)
