"""Application service assembling a complete DailyProfile."""

from datetime import date

from vitalis.storage import HealthRepository, session_scope

from .analyzers import HrvAnalyzer, RecoveryAnalyzer, SleepAnalyzer, TrainingAnalyzer, build_states
from .baseline import BaselineEngine
from .contracts import DailyProfile, EvidenceRef, ProfileFeatures
from .decision import DecisionEngine
from .profile import ProfileLoader


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


class IntelligenceService:
    def daily_profile(self, user_id: str, day: date | None = None) -> DailyProfile:
        target = day or date.today()
        with session_scope() as db:
            repo = HealthRepository(db)
            raw = ProfileLoader(repo).load(user_id, target)
            identity = repo.identity_context(user_id)

        baselines = BaselineEngine().build(raw.series, target)
        sleep, sleep_state = SleepAnalyzer().analyze(raw, baselines)
        hrv = HrvAnalyzer().analyze(raw, baselines)
        training = TrainingAnalyzer().analyze(raw, baselines)
        recovery = RecoveryAnalyzer().analyze(raw, sleep, sleep_state, hrv, training)
        decision = DecisionEngine().decide(sleep_state, hrv, recovery, training)
        return DailyProfile(
            user_id=user_id,
            date=target,
            data_quality=raw.data_quality,
            facts=raw.facts,
            baselines=baselines,
            features=ProfileFeatures(sleep=sleep, hrv=hrv, recovery=recovery, training=training),
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
