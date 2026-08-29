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

- Add exercise-prescription evidence references before tightening training-plan
  rules. Candidate ids: `ACSM_RT_2026`, `CONCURRENT_TRAINING_2022`,
  `RIR_RPE_VALIDITY_2024`, and `RIR_ACCURACY_2026`.
- Keep exact same-day spacing, 48-hour lower-body conflict windows, and threshold
  interval templates as versioned product policy unless future evidence justifies
  more specific claims.
- Extend `ActionPlan` later with structured evidence ids per planned session,
  rather than relying only on prose evidence strings.
- Add a user-facing RIR familiarization path before using RIR to raise plan
  confidence or prescribe higher effort.
- Preserve current device boundaries: manufacturer capability claims may inform
  labels and setup guidance, but not device-specific accuracy status.
- Continue treating dense `SEC_HR` files as coverage metadata until decoded and
  validated against known sample semantics.

### Candidate Next Work

1. Evidence Library 2026-08b: add the exercise-prescription evidence refs to
   `EVIDENCE_REFS`, `skills/vitalis/knowledge/evidence.md`, and Architecture.
2. ActionPlan Evidence v1.1: add `evidence_ref_ids` or rule/evidence bindings to
   planned sessions while keeping existing Chinese strings for rendering.
3. RIR Feedback UX: add a guided feedback command for RPE/RIR, soreness, and
   completion quality, with experience-aware limitations.
4. Concurrent Policy Audit: turn the current 6-hour and 48-hour scheduling
   numbers into explicit named policy constants with tests and documentation.
5. Zepp Capability Monitor: periodically re-check official Zepp OS docs and
   product pages for changed API_LEVEL, heart-rate, workout, and device claims.

