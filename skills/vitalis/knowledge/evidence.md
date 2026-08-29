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
  therefore compares the same HRV metric within each device baseline and fuses only
  normalized directions; it never averages raw RMSSD/SDNN across devices.

Device validity remains `LIMITED_BY_EVIDENCE` when only form-factor research exists.
Never convert missing model-specific validation or indexed-but-undecoded second-heart-
rate files into a synthetic quality probability.
