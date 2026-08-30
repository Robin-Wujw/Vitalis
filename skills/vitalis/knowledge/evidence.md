# Evidence Scope

The API response is authoritative for evidence references used by a specific profile.
The first model version uses the following boundaries:

- WHO physical activity guidance supports weekly population-level aerobic and strength
  context; it is not an individual readiness formula.
- The 1996 HRV standards support measurement separation and interpretation discipline;
  they do not validate a Vitalis recovery score.
- World Sleep Society guidance requires consumer sleep stages to be treated as trend
  indicators rather than polysomnography-equivalent truth.
- The AASM/SRS adult sleep consensus supports at least seven hours as a general adult
  duration reference, with individual context still required.
- IOC load guidance supports integrated load, recovery, health, and wellbeing
  monitoring; it does not justify a universal acute/chronic ratio threshold.
- The 2026 ACSM resistance-training position stand supports progressive resistance
  training, major-muscle-group coverage, and manipulation of load, volume, range of
  motion, power intent, and weekly sets for healthy adults. It supports movement-
  pattern-level prescriptions; it does not let Vitalis infer absolute weight targets
  without individual strength history.
- Concurrent-training reviews support cautious scheduling when aerobic and strength
  work are both due, especially around same-session explosive-strength outcomes.
  Current six-hour spacing and 48-hour lower-body conflict windows are Vitalis product
  policy, not directly proven universal thresholds.
- RPE/RIR research supports guided subjective effort feedback for resistance training,
  but accuracy depends on experience, load, exercise type, proximity to failure, and
  user familiarization. RPE/RIR is user-reported context, not a precise substitute for
  measured load or completed sets/reps/weight.
- Hettiarachchi et al. (2019, DOI `10.1371/journal.pone.0217288`) found close
  agreement between an upper-arm Polar OH1 PPG sensor and ECG during moderate- to
  high-intensity exercise. A 2025 arm-versus-wrist validation study
  (`10.2196/67110`) also found better aggregate heart-rate agreement for its tested
  upper-arm sensor. These studies support the upper-arm form factor for exercise
  heart rate; they are not independent validation of Amazfit Helio Strap.
- Bent et al. (2020, DOI `10.1038/s41746-020-0226-6`) found PPG error varied by
  device and activity, with activity error higher than rest. Sampling frequency or
  wearing position alone must not be converted into a universal accuracy claim.
- Schäfer and Vagedes (2013, DOI `10.1016/j.ijcard.2012.03.119`) found pulse-rate
  variability is not interchangeable with ECG-derived HRV in every setting. Vitalis
  therefore compares the same HRV metric within each device baseline and never averages
  raw RMSSD/SDNN across devices.
- Dial et al. (2025, DOI `10.14814/phy2.70527`) found materially different nocturnal
  HRV agreement across five consumer devices measured against ECG. This supports
  model-specific validation and stable within-device baselines, not choosing a sleep
  HRV source from wearing position alone.
- Bland and Altman's method-comparison guidance (1986, DOI
  `10.1016/S0140-6736(86)90837-8`) requires demonstrated agreement before measurement
  methods are treated as interchangeable. A secondary wearable can corroborate the
  canonical stream, but agreement does not turn two unvalidated streams into a more
  accurate synthetic value.
- Laborde et al. (2017, DOI `10.3389/fpsyg.2017.00213`), the 2024 Society for
  Psychophysiological Research guidance (DOI `10.1111/psyp.14604`), and Sammito et al.
  (2024, DOI `10.3389/fphys.2024.1430458`) require ambulatory HRV interpretation to
  account for recording method, activity, posture, time, physiology, lifestyle, and
  external confounders. A minute-scale wearable value is not a direct stress label.
- Järvelin-Pasanen et al. (2018, DOI `10.2486/indhealth.2017-0190`) supports an
  association between occupational stress and reduced parasympathetic HRV measures at
  group and observation-window levels. It does not validate classifying each low
  daytime RMSSD point as stress.
- Saygin et al. (2025, DOI `10.1016/j.biopsycho.2025.109171`) shows that respiration
  can create spurious ambulatory RSA associations. Schneider et al. (2025, DOI
  `10.1007/s11886-025-02299-4`) found context-dependent positive-affect/HRV directions.
  Vitalis therefore requires contextual and subjective evidence rather than inferring
  stress valence or happiness from daytime HRV alone.

Device validity remains `LIMITED_BY_EVIDENCE` when only form-factor research exists.
Never convert missing model-specific validation or indexed-but-undecoded second-heart-
rate files into a synthetic quality probability.

For exercise heart rate, a decoded and explicitly attributed upper-arm stream is the
preferred context when available because the form factor has stronger activity evidence.
For nocturnal HRV, Vitalis instead selects one canonical same-metric stream by usable
baseline coverage and continuity. Secondary streams are retained for audit and silent
corroboration. They reduce confidence only when a comparable within-device direction
disagrees; they do not replace the canonical conclusion.
