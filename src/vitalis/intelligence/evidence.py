"""Evidence references used by deterministic intelligence policies."""

from .contracts import EvidenceRef


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
        id="ARM_PPG_OH1_2019",
        title="Validation of Polar OH1 optical heart rate sensor during exercise",
        url="https://pubmed.ncbi.nlm.nih.gov/31120968/",
        applies_to=["upper_arm_ppg_heart_rate", "measurement_site"],
    ),
    EvidenceRef(
        id="ARM_WRIST_PPG_2025",
        title="Wrist-worn and arm-worn wearables for monitoring heart rate",
        url="https://pubmed.ncbi.nlm.nih.gov/40116771/",
        applies_to=["upper_arm_ppg_heart_rate", "measurement_site"],
    ),
    EvidenceRef(
        id="WRIST_PPG_META_2020",
        title="Validity of wrist-worn PPG devices to measure heart rate",
        url="https://doi.org/10.1080/02640414.2020.1767348",
        applies_to=["wrist_ppg_heart_rate", "activity_specific_accuracy"],
    ),
    EvidenceRef(
        id="PPG_ERROR_SOURCES_2020",
        title="Investigating sources of inaccuracy in wearable optical heart rate sensors",
        url="https://doi.org/10.1038/s41746-020-0226-6",
        applies_to=["ppg_motion_artifact", "device_specific_accuracy"],
    ),
    EvidenceRef(
        id="PRV_HRV_REVIEW_2013",
        title="How accurate is pulse rate variability as an estimate of heart rate variability?",
        url="https://doi.org/10.1016/j.ijcard.2012.03.119",
        applies_to=["ppg_hrv_limitations", "device_fusion"],
    ),
    EvidenceRef(
        id="NOCTURNAL_WEARABLE_HRV_2025",
        title="Validation of nocturnal resting heart rate and heart rate variability in consumer wearables",
        url="https://pubmed.ncbi.nlm.nih.gov/40834291/",
        applies_to=["nocturnal_hrv", "device_specific_accuracy", "device_fusion"],
    ),
    EvidenceRef(
        id="MEASUREMENT_AGREEMENT_1986",
        title="Statistical methods for assessing agreement between two methods of clinical measurement",
        url="https://doi.org/10.1016/S0140-6736(86)90837-8",
        applies_to=["measurement_agreement", "device_interchangeability"],
    ),
    EvidenceRef(
        id="FDA_PULSE_OXIMETER_LIMITATIONS",
        title="Pulse Oximeter Accuracy and Limitations",
        url="https://www.fda.gov/medical-devices/safety-communications/pulse-oximeter-accuracy-and-limitations-fda-safety-communication",
        applies_to=["wearable_spo2", "oxygen_measurement_limitations"],
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
    EvidenceRef(
        id="ACSM_RESISTANCE_TRAINING_2026",
        title="ACSM position stand on resistance training for health, fitness, and performance",
        url="https://doi.org/10.1249/MSS.0000000000003897",
        applies_to=[
            "resistance_training_prescription",
            "major_muscle_groups",
            "progressive_overload",
        ],
    ),
    EvidenceRef(
        id="CONCURRENT_TRAINING_2022",
        title="Compatibility of concurrent aerobic and strength training adaptations",
        url="https://pmc.ncbi.nlm.nih.gov/articles/PMC8891239/",
        applies_to=[
            "concurrent_training_context",
            "aerobic_strength_scheduling",
        ],
    ),
    EvidenceRef(
        id="RIR_RPE_SCALE_2016",
        title="Novel resistance training-specific rating of perceived exertion scale",
        url="https://pubmed.ncbi.nlm.nih.gov/26049792/",
        applies_to=[
            "subjective_effort_feedback",
            "repetitions_in_reserve",
        ],
    ),
    EvidenceRef(
        id="RIR_ACCURACY_REVIEW_2026",
        title="Systematic review of repetitions-in-reserve accuracy in resistance training",
        url="https://doi.org/10.1080/10833196.2025.2564026",
        applies_to=[
            "repetitions_in_reserve_limitations",
            "subjective_effort_feedback",
        ],
    ),
]
