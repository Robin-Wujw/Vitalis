# Research Notes

This document records local research findings that may inform future Vitalis contracts.
It is not itself a product specification. Code changes still need their own
`SYSTEM.md` checklist, tests, documentation, and delivery commit.

## 2026-08-29: Exercise Prescription And Device Evidence

### Scope

The latest implementation already provides DailyProfile 4.0, Decision Policy 4.0,
running analysis, strength analysis, and a health-first concurrent `ActionPlan`.
This pass reviewed whether the current evidence library is strong enough for the
next iteration of:

- resistance-training prescription details;
- running and strength scheduling when both are due;
- RPE/RIR-based subjective feedback;
- Balance 2 / Helio Strap device and Zepp OS capability claims.

### Sources Reviewed

Primary or near-primary sources:

- WHO 2020 physical activity guidance:
  <https://www.who.int/publications/i/item/9789240015128>
- WHO adult recommendation summary:
  <https://www.ncbi.nlm.nih.gov/books/NBK566046/>
- ACSM 2026 resistance-training position stand:
  <https://doi.org/10.1249/MSS.0000000000003897>
- Wilson et al. 2012 concurrent training meta-analysis:
  <https://pubmed.ncbi.nlm.nih.gov/22002517/>
- Schumann et al. 2022 updated concurrent-training meta-analysis:
  <https://pmc.ncbi.nlm.nih.gov/articles/PMC8891239/>
- Zourdos et al. 2016 RIR-based RPE scale:
  <https://pubmed.ncbi.nlm.nih.gov/26049792/>
- Helms et al. 2016 RIR practical application:
  <https://pmc.ncbi.nlm.nih.gov/articles/PMC4961270/>
- RPE / movement-velocity systematic review:
  <https://pubmed.ncbi.nlm.nih.gov/38910451/>
- Russo et al. 2026 RIR accuracy systematic review:
  <https://doi.org/10.1080/10833196.2025.2564026>
- Zepp OS HeartRate API:
  <https://docs.zepp.com/docs/reference/device-app-api/newAPI/sensor/HeartRate/>
- Zepp OS Workout API:
  <https://docs.zepp.com/docs/reference/device-app-api/newAPI/sensor/Workout/>
- Zepp OS Side Service Fetch API:
  <https://docs.zepp.com/docs/reference/side-service-api/fetch/>
- Zepp OS System Events:
  <https://docs.zepp.com/docs/guides/framework/device/system-event/>
- Amazfit Balance 2 product notes:
  <https://us.amazfit.com/products/balance-2>
- Amazfit Helio Strap product notes:
  <https://us.amazfit.com/products/helio-strap>
- Amazfit Helio Strap wearing-position FAQ:
  <https://in.amazfit.com/pages/faq/where-is-the-best-place-to-wear-the-helio-strap-for-accuracy>

### Findings

WHO supports Vitalis' product-level requirement that aerobic activity and
muscle-strengthening both remain in the plan. It supports weekly context, not
readiness scoring, exact session sequencing, or an individual prescription on a
given day.

The 2026 ACSM position stand is now the strongest broad evidence source for
healthy-adult resistance-training prescription. It supports progressive resistance
training at least twice per week, engaging major muscle groups, with manipulable
variables such as load, volume, range of motion, power intent, and weekly sets.
It also explicitly leaves room for individualization and participation/adherence.
For Vitalis, this supports generic movement-pattern prescriptions and progression
rules, but not automatic absolute weights without user-provided strength history.

The concurrent-training evidence is more nuanced than the current simple conflict
model. Wilson et al. found interference patterns affected by endurance modality,
duration, and frequency, with running more problematic than cycling in that older
analysis. Schumann et al. found no clear compromise to maximal strength or
whole-muscle hypertrophy overall, but did find concern for explosive-strength
development, especially when aerobic and strength work are performed in the same
session. Vitalis' "easy run plus upper strength may be an addition, quality run
plus lower strength is an alternative" rule is directionally defensible, but the
exact six-hour addition gap and 48-hour conflict window remain product policy and
should be labeled that way unless stronger evidence is attached.

RPE/RIR is useful for subjective load regulation, especially for resistance
training, but accuracy depends on experience, proximity to failure, load, exercise
type, and user familiarization. Vitalis should continue to store RPE/RIR as
user-reported context rather than converting it into a precise load estimate. A
future feedback flow should teach the scale by asking concrete questions such as
"how many clean reps did you have left?" rather than assuming every user already
understands RIR.

Zepp OS confirms the local Balance 2 bridge boundary: heart-rate APIs expose
minute history, current/last values, callbacks, workout status/history, user HR
zone settings from API_LEVEL 4.2, system events, and Side Service `fetch`. The
docs do not establish a guaranteed continuous callback frequency. The current
bridge language that avoids fixed-frequency claims is correct.

Amazfit's current product pages support only product-capability statements:
Balance 2 exposes BioTracker 6.0 PPG, sleep HRV, training load, recovery time,
and configurable heart-rate monitoring intervals; Helio Strap supports wrist or
upper-arm wear, every-second tracking claims, live heart-rate broadcast, and
training/recovery metrics. These are manufacturer claims, not independent
model-specific validation. No peer-reviewed Balance 2 or Helio Strap validation
study was found in this pass, so `device_validity.status` should remain
`UNKNOWN` or evidence-limited rather than upgraded.

### Product Implications

- Bind exercise-prescription rules to the implemented `ACSM_RESISTANCE_TRAINING_2026`,
  `CONCURRENT_TRAINING_2022`, `RIR_RPE_SCALE_2016`, and
  `RIR_ACCURACY_REVIEW_2026` evidence records.
- Keep exact same-day spacing, 48-hour lower-body conflict windows, and threshold
  interval templates as versioned product policy unless future evidence justifies
  more specific claims.
- Keep the structured evidence ids on each planned session synchronized with rule
  changes rather than relying only on prose evidence strings.
- Add a user-facing RIR familiarization path before using RIR to raise plan
  confidence or prescribe higher effort.
- Preserve current device boundaries: manufacturer capability claims may inform
  labels and setup guidance, but not device-specific accuracy status.
- Keep decoded `SEC_HR` streams device-isolated. Use their values for sleep-window and
  exercise heart-rate analysis only; do not reinterpret one-second pulse rate as
  beat-to-beat intervals or derive HRV from it.

### Implemented In Intelligence 7.0

- `PlannedSession.evidence_ref_ids` now binds running and resistance prescriptions to
  the evidence library instead of leaving the sources as detached metadata.
- Running dose uses recent personal session duration, personal threshold HR, recent
  hard-session timing, cardiac drift, cadence context, and concurrent lower-body load.
- Strength dose reuses explicit exercise, set, repetition, load, rest, and RPE/RIR facts
  when available. Heart-rate structure still cannot identify a specific exercise.
- Ordinary minute heart rate is analyzed only inside recorded sleep windows, with
  coverage gates, same-device 28-night comparison, and a separate same-device HRV
  seven-day comparison. It is not used to reconstruct beat-to-beat HRV.
- The official Zepp Android contract was verified as file index -> signed HTTPS ZIP ->
  `DailySecondHeartBeat` protobuf. Consecutive integer heart-rate values are decoded at
  one-second spacing, `255` is discarded as missing, archive entries are mapped to
  device intervals by a one-to-one overlap assignment, and decoded rows are skipped on
  later syncs. Nightly profiles query only actual sleep windows and aggregate each
  device to minute medians before analysis.

## 2026-08-30: Multi-device HRV Fusion And Daily Reporting

### Sources Reviewed

- Task Force HRV measurement standards (1996):
  <https://pubmed.ncbi.nlm.nih.gov/8737210/>
- Schäfer and Vagedes review of PPG pulse-rate variability versus ECG HRV (2013):
  <https://doi.org/10.1016/j.ijcard.2012.03.119>
- Hettiarachchi et al. upper-arm Polar OH1 exercise validation (2019):
  <https://pubmed.ncbi.nlm.nih.gov/31120968/>
- Schweizer and Gilgen-Ammann arm-versus-wrist exercise validation (2025):
  <https://pubmed.ncbi.nlm.nih.gov/40116771/>
- Dial et al. five-device nocturnal RHR/HRV validation (2025):
  <https://pubmed.ncbi.nlm.nih.gov/40834291/>
- Bland and Altman measurement-agreement method (1986):
  <https://doi.org/10.1016/S0140-6736(86)90837-8>
- PubMed searches for `Amazfit Helio Strap` and `Amazfit Balance 2`, completed
  2026-08-30, returned no model-specific validation records.

### Findings And Policy

The exercise-heart-rate and nocturnal-HRV questions require different source policies.
Upper-arm PPG performed better than wrist PPG in the reviewed exercise studies, but
those results belong to the tested Polar models. They support a form-factor preference
for decoded, explicitly attributed exercise heart rate; they do not independently
validate Helio Strap or prove that every-second recording is more accurate.

Nocturnal HRV performance varied materially across tested consumer models even under
low-motion sleep conditions. Consequently, wearing position alone is not an adequate
selection rule for sleep HRV. Vitalis should select one canonical same-metric stream by
usable 28-day coverage, current observation density, and continuity. It should compare
that stream only with its own baseline.

Different devices and proprietary algorithms have not demonstrated interchangeability.
Raw HRV milliseconds must not be averaged. Secondary devices remain audit evidence and
silent corroboration: agreement does not inflate confidence, while a comparable
directional disagreement can reduce confidence. The report should mention such a
disagreement only when it weakens a recovery-dependent recommendation.

The daily report is a presentation layer, not an audit dump. Full per-device streams,
multi-window trends, limitations, and rule evidence remain in DailyProfile. The push
should select only facts that explain or change today's action, omit empty/unknown
sections, and translate any retained event into its practical implication.

## 2026-08-30: Ambulatory Daytime HRV Interpretation

### Sources Reviewed

- Task Force short-term HRV measurement standards (1996):
  <https://pubmed.ncbi.nlm.nih.gov/8737210/>
- Laborde et al. psychophysiological HRV recommendations (2017):
  <https://doi.org/10.3389/fpsyg.2017.00213>
- Society for Psychophysiological Research publication guidance, including ambulatory
  ECG/PPG measurement (2024): <https://doi.org/10.1111/psyp.14604>
- Sammito et al. review of physiological, disease, lifestyle, and external HRV
  confounders (2024): <https://doi.org/10.3389/fphys.2024.1430458>
- Järvelin-Pasanen et al. occupational stress and HRV systematic review (2018):
  <https://doi.org/10.2486/indhealth.2017-0190>
- Vrijkotte et al. activity- and posture-adjusted ambulatory work-stress study (2000):
  <https://doi.org/10.1161/01.HYP.35.4.880>
- Saygin et al. ambulatory respiratory-control study (2025):
  <https://doi.org/10.1016/j.biopsycho.2025.109171>
- Schneider et al. positive affect and HRV systematic review (2025):
  <https://doi.org/10.1007/s11886-025-02299-4>

### Findings And Product Boundary

Ambulatory vagally mediated HRV can contribute information about physiological arousal,
but a momentary RMSSD value is not a direct stress or emotion measurement. Occupational
stress studies commonly associate higher chronic stress with lower RMSSD or other
parasympathetic measures, while using questionnaires and longer observation windows.
This evidence does not validate labeling each low daytime wearable value as stress.

Posture, physical activity, respiration, time of day, recent food/caffeine/alcohol,
temperature, illness, and measurement quality can all change HRV. Activity- and
posture-adjusted ambulatory studies demonstrate why the metabolic context must be
separated from non-metabolic changes. Respiratory behavior can also create spurious
within-person psychological associations when it is not measured or controlled.

Positive affect has a context-dependent association with vagally mediated HRV. The
reviewed evidence found different directions for trait, resting, activated, and
momentary positive affect. Vitalis must therefore never translate an HRV segment into
"happy". Emotion claims require time-linked subjective reports such as ecological
momentary assessment.

The real Zepp `HRVRMSSD/real_data` stream contains attributed daytime samples, but it is
not continuous: samples occur in minute-spaced runs separated by long gaps, and the
current day may be uploaded only through the morning even after a later synchronization.
Missing intervals are unknown coverage, not high stress.

### Analysis Policy

- Keep nocturnal recovery HRV and daytime ambulatory HRV as separate contracts.
- Preserve one canonical same-device RMSSD stream and compare `lnRMSSD` only with that
  stream's own history; do not merge wearable milliseconds.
- Display gap-aware descriptive daytime coverage before making interpretations. Do not
  connect segments across missing intervals or substitute zeros.
- Aggregate only sufficiently sampled stationary windows. Five-minute HRV standards
  inform the minimum observation concept, but Zepp's proprietary per-sample calculation
  window must be verified before selecting the final window rule.
- Build time-of-day personal references so circadian variation is not treated as a
  stress response.
- Require time-aligned low-activity/posture context and exclude workouts before emitting
  a "lower physiological load" candidate. Current daily steps and workout summaries are
  not enough for this gate.
- Treat respiration as an explicit confidence limitation until a suitable daytime
  respiratory stream exists.
- Require subjective mood input before learning or reporting personal emotion
  associations. Never infer happiness from HRV alone.

The descriptive first stage is implemented: Zepp's nightly `sleepHRV`, sleep-oriented
`hrv_sdnn`, and timestamped `HRVRMSSD` streams remain separate. Recovery prefers the
nightly summary. SDNN is summarized per local calendar day using the mean, measured
minimum/maximum and sample count used by ZeppBridge's daily trend. The report plots seven
daily SDNN means from one canonical device stream and does not merge absolute HRV values
across devices. Timestamped RMSSD remains in the structured daily profile, but its
sleep-only curve is omitted from the evening report. Sparse daytime RMSSD records stay
available in storage and do not appear as an invented all-day curve. Minute data does not
establish beat-to-beat or ECG equivalence, and neither HRV stream is converted into a
stress, recovery, or emotion claim;
context-dependent daytime interpretation remains blocked by the activity, posture,
respiration, and time-of-day reference requirements above.
