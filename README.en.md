# Vitalis

[中文](README.md) | [Documentation](docs/README.md)

> Turn wearable data into daily, actionable, and explainable health and training decisions.

Vitalis is a personal health intelligence engine built around longitudinal data. It connects to wearable platforms, continuously synchronizes sleep, HRV, resting heart rate, activity, training, and subjective feedback, builds individual baselines, and produces Daily, Weekly, Monthly, and training-response analysis.

Vitalis does not ask an LLM to improvise over raw health data. Health state, trends, events, and training decisions are computed by a deterministic engine. Hermes or another agent only invokes structured results, explains persisted evidence, and records feedback explicitly provided by the user.

## From Data to Action

```text
Zepp / Balance 2 / Helio Strap
              |
              v
 Durable sync and normalized storage
              |
              v
Quality -> Personal baselines -> Trends and events -> Training decision
              |
              v
 Morning / Evening / Weekly / Monthly
              |
              v
      API, Hermes Skill, PushPlus
```

A typical workflow:

1. Import credentials through the official Zepp login page and browser pairing without sending an account password to Vitalis.
2. The API and independent worker fetch and normalize health data through a durable synchronization ledger.
3. The deterministic intelligence pipeline evaluates current state, recent trends, training load, and historical responses.
4. The API, Hermes, and PushPlus render the same structured conclusions instead of recalculating them independently.

## Current Capabilities

| Area | Implemented |
| --- | --- |
| Data ingestion | Zepp cloud history, official browser pairing, and supplemental Balance 2 Zepp OS uploads |
| Personal baselines | Individual ranges and deviations for sleep, HRV, resting heart rate, activity, and training load |
| Training decisions | Health-first running, strength, recovery, or rest guidance with concrete session dosage |
| Periodic analysis | Daily, Weekly, fixed 28-day Monthly, 7/28/90-day trends, and health-event lifecycle |
| Training loop | Recommendation, completed workout, subjective feedback, T+1/T+2/T+3 response, and personal history |
| Open insights | Auditable readiness, sleep regularity, TRIMP, and ATL/CTL/TSB kept shadow-only |
| Agent integration | Hermes Read / Analyze / Act tools, explanation workflows, and PushPlus delivery |
| Operations | SQLite/PostgreSQL, durable sync attempts, worker entry point, systemd units, and data-health reporting |

The [architecture document](docs/ARCHITECTURE.md) is authoritative for data contracts, algorithm boundaries, coverage gates, and refusal behavior.

## Trust Boundaries

- **Facts, inference, and advice remain distinct.** Device observations, system conclusions, and actions retain separate semantics and provenance.
- **Missing data stays missing.** Vitalis does not fill required observations with zeroes, stale snapshots, vendor scores, or template advice.
- **Devices and identities remain isolated.** Measurements retain source, scope, device, and unit; one Zepp vendor identity has one local owner.
- **Personal baselines come first.** The engine evaluates change against individual history instead of treating one population threshold as a personal conclusion.
- **Agents do not recompute health facts.** They consume versioned structured outputs and cannot invent trends, scores, or prescriptions.
- **Vitalis is not a medical device.** It supports personal trend review and exercise decisions; it does not diagnose disease or replace clinical judgment.

## Project Status

Vitalis is a runnable, actively developed pre-production project. Its first complete device ecosystem is Zepp with Balance 2 and Helio Strap. Current work prioritizes data correctness, recoverable synchronization, transparent decisions, training-feedback continuity, and verifiable deployment. Opaque composite health scores or predictive models will not replace the current decision policy without separate evidence, versioning, and rollback controls.

Read the deployment, backup, identity-migration, and recovery requirements before using Vitalis with long-lived data. Do not expose development defaults directly to the public internet.

## Start Here

- [Documentation hub](docs/README.md): navigation by user, integrator, and maintainer role
- [Getting started](docs/GETTING_STARTED.md): installation, local runtime, service deployment, scheduling, and verification
- [Zepp integration](docs/ZEPP_INTEGRATION.md): official login, credential lifecycle, identity ownership, and data coverage
- [API guide](docs/API.md): current HTTP surface and identity requirements
- [Architecture](docs/ARCHITECTURE.md): data flow, intelligence pipeline, policy boundaries, and versioned contracts
- [Hermes Skill](skills/vitalis/SKILL.md): Read / Analyze / Act boundaries for agents
