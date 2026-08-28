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

Device validity is `UNKNOWN` unless the profile includes device-specific evidence.
Never convert missing validation evidence into a synthetic quality probability.
