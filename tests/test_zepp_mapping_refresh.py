"""Catalog corrections retain observations and invalidate affected analyses."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.adapters.persistence import models as orm
from vitalis.adapters.persistence.source_journal import SourceJournalRepository, SourceRecordInput
from vitalis.domain import Workout, WorkoutMetricSample, WORKOUT_DETAIL_SCHEMA_VERSION
from vitalis.intelligence.contracts import (
    DAILY_SCHEMA_VERSION, DECISION_POLICY_VERSION, EVIDENCE_VERSION, INTELLIGENCE_VERSION,
)
from vitalis.time import local_day
from vitalis.zepp_mapping_refresh import refresh_zepp_mappings


def test_refresh_preserves_facts_and_queues_historical_corrections():
    user = "catalog-refresh-history"
    started = datetime.now(timezone.utc) - timedelta(days=30)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user)
        repo.save_workout(Workout(
            user_id=user, workout_id="free", started_at=started, duration=48,
            vendor_type_id=16, vendor_type_namespace="zepp.sport.cloud_history",
            sport_mode="unknown_16", calories=325, heart_rate_avg=112,
            vendor_source="original-vendor-source",
        ))
        repo.save_workout(Workout(user_id=user, workout_id="ambiguous", duration=1, vendor_type_id=16))
        detail = {"schema_version": WORKOUT_DETAIL_SCHEMA_VERSION, "strength_sets": [
            {"source": "lap_62", "vendor_exercise_code": 1, "exercise_name": None,
             "repetitions": 9, "weight_value": 17.5, "weight_unit": None,
             "limitations": ["exercise_name_unverified", "weight_unit_unverified"]},
            {"source": "lap_62", "vendor_exercise_code": 14, "exercise_name": "personal",
             "limitations": ["user_confirmed"]},
            {"source": "lap_62", "vendor_exercise_code": 999999, "exercise_name": None,
             "limitations": ["exercise_name_unverified"]},
        ]}
        repo.save_workout_detail(user, "free", detail, fetched_at=started)
        before = deepcopy(repo.workout(user, "free").data)
        before_detail = deepcopy(repo.workout(user, "free").detail)
        revision = repo.analysis_input_revision(user)
        preview = refresh_zepp_mappings(db, user)
        assert preview["workouts_to_reclassify"] == 1
        assert preview["sets_to_label"] == 1
        assert preview["namespace_unresolved"] == 1
        assert repo.analysis_input_revision(user) == revision
        assert repo.workout(user, "free").data == before
        applied = refresh_zepp_mappings(db, user, apply=True)
        assert applied["mode"] == "applied"
        row = repo.workout(user, "free")
        assert row.data["sport_mode_label"] == "自由训练"
        for field in ("duration", "calories", "heart_rate_avg", "started_at", "vendor_source"):
            assert row.data[field] == before[field]
        assert row.detail["fetched_at"] == before_detail["fetched_at"]
        assert row.detail["strength_sets"][0]["exercise_name"]
        assert row.detail["strength_sets"][0]["weight_unit"] is None
        assert row.detail["strength_sets"][1:] == before_detail["strength_sets"][1:]
        jobs = repo.analysis_jobs(user)
        assert any(job.event_type == "catalog_refresh" and job.target_date == local_day(started) for job in jobs)
        assert repo.analysis_input_revision(user) > revision
        again = refresh_zepp_mappings(db, user, apply=True)
        assert again["workouts_to_update"] == 0


@pytest.mark.parametrize("vendor_code,source", [
    (16, "zepp_cloud_observed"),
    (5, "public_zepp_enum"),
])
def test_correct_legacy_classification_retains_original_evidence(vendor_code, source):
    user = f"catalog-correct-legacy-{vendor_code}"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user)
        repo.save_workout(Workout(
            user_id=user, workout_id="correct", duration=30, type="other",
            sport_mode="free_training", sport_mode_label="自由训练", training_family="mixed",
            vendor_type_id=vendor_code, recognition_source=source,
            recognition_confidence="MEDIUM", recognition_source_label="original verified evidence",
        ))
        before = deepcopy(repo.workout(user, "correct").data)
        revision = repo.analysis_input_revision(user)
        result = refresh_zepp_mappings(db, user, apply=True)
        assert result["workouts_to_update"] == 0
        assert result["namespace_unresolved"] == 0
        assert repo.workout(user, "correct").data == before
        assert repo.analysis_input_revision(user) == revision


def test_refresh_retains_unknown_fields_samples_and_protected_names():
    user = "catalog-preserve-all-observations"
    started = datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user)
        repo.save_workout(Workout(
            user_id=user, workout_id="free", started_at=started, duration=42,
            vendor_type_id=16, recognition_source="zepp_cloud_observed",
            calories=0, heart_rate_avg=None, load=None,
            observed_fields=["calories"], vendor_source="vendor-detail-source",
        ))
        row = repo.workout(user, "free")
        row.data = dict(row.data) | {"future_observation": {"unit": "unknown", "value": None}}
        detail = {
            "schema_version": WORKOUT_DETAIL_SCHEMA_VERSION,
            "future_detail": {"observed": 0, "qualification": "unverified"},
            "strength_sets": [
                {"source": "lap_62", "vendor_exercise_code": 1, "exercise_name": None,
                 "repetitions": 0, "weight_value": None,
                 "limitations": ["exercise_name_unverified", "weight_unit_unverified"]},
                {"source": "lap_62", "vendor_exercise_code": 14, "exercise_name": "custom name",
                 "limitations": ["exercise_name_unverified"]},
                {"source": "lap_62", "vendor_exercise_code": 14, "exercise_name": "confirmed name",
                 "limitations": ["exercise_name_reference_mapping", "user_confirmed"]},
                {"source": "lap_62", "vendor_exercise_code": 999999, "exercise_name": None,
                 "limitations": ["exercise_name_unverified"]},
            ],
        }
        repo.save_workout_detail(user, "free", detail, fetched_at=started, samples=[
            WorkoutMetricSample(
                workout_id="free", timestamp=started, metric="heart_rate", value=0,
                unit="bpm", source_scope="synthetic-device", device_id="device-a", sample_ordinal=7,
            ),
            WorkoutMetricSample(
                workout_id="free", timestamp=started + timedelta(seconds=1),
                metric="heart_rate", value=123, unit="bpm",
                source_scope="synthetic-device", device_id="device-a", sample_ordinal=8,
            ),
        ])
        before_data, before_detail = deepcopy(row.data), deepcopy(row.detail)
        sample_columns = tuple(orm.WorkoutMetricSample.__table__.columns.keys())
        before_samples = [
            tuple(getattr(sample, key) for key in sample_columns)
            for sample in repo.workout_metric_samples(user, "free")
        ]
        result = refresh_zepp_mappings(db, user, apply=True)
        assert result["workouts_to_reclassify"] == 1
        assert result["sets_to_label"] == 1
        after = repo.workout(user, "free")
        allowed = {
            "type", "sport_mode", "sport_mode_label", "training_family", "recognition_confidence",
            "recognition_confidence_label", "recognition_source", "recognition_source_label",
            "vendor_type_namespace",
        }
        assert {key: value for key, value in after.data.items() if key not in allowed} == {
            key: value for key, value in before_data.items() if key not in allowed
        }
        expected_detail = deepcopy(before_detail)
        expected_detail["strength_sets"][0]["exercise_name"] = after.detail["strength_sets"][0]["exercise_name"]
        expected_detail["strength_sets"][0]["limitations"] = [
            "weight_unit_unverified", "exercise_name_reference_mapping",
        ]
        assert expected_detail["strength_sets"][0]["exercise_name"]
        assert after.detail == expected_detail
        assert [
            tuple(getattr(sample, key) for key in sample_columns)
            for sample in repo.workout_metric_samples(user, "free")
        ] == before_samples


def test_raw_journal_namespace_requires_owner_and_unambiguous_endpoint():
    user, other = "catalog-journal-owner", "catalog-journal-other"
    observed = datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
    with session_scope() as db:
        repo = HealthRepository(db)
        journal = SourceJournalRepository(db)
        for owner in (user, other):
            repo.upsert_user(owner)
            repo.bind_source_mode(owner, "mock")
        for workout_id in ("proven", "foreign", "conflicting"):
            repo.save_workout(Workout(
                user_id=user, workout_id=workout_id, duration=10, vendor_type_id=16,
                vendor_source="zepp_cloud_observed",
            ))
        for owner, endpoint, workout_ids in (
            (user, "run", ("proven", "conflicting")),
            (other, "run", ("foreign",)),
            (user, "ride", ("conflicting",)),
        ):
            journal.append(SourceRecordInput(
                user_id=owner, source="zepp", source_mode="mock", stream="workouts",
                record_id=f"sport_history:{endpoint}:1770000000:1770100000",
                observed_start=observed, observed_end=observed + timedelta(days=1), fetched_at=observed,
                payload={"data": {"summary": [{"trackid": item, "type": 16} for item in workout_ids]}},
            ))
        result = refresh_zepp_mappings(db, user, apply=True)
        assert result["workouts_to_reclassify"] == 1
        assert result["namespace_unresolved"] == 2
        assert repo.workout(user, "proven").data["sport_mode"] == "free_training"
        assert repo.workout(user, "proven").data["vendor_type_namespace"] == "zepp.sport.cloud_history"
        for workout_id in ("foreign", "conflicting"):
            assert repo.workout(user, workout_id).data["sport_mode"] == "unknown"


def test_historical_classification_targets_only_affected_window_and_retains_snapshots():
    user = "catalog-scoped-history"
    started = datetime.now(timezone.utc) - timedelta(days=250)
    day = local_day(started)
    offsets = (-1, 0, 10, 179, 180)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user)
        repo.save_workout(Workout(
            user_id=user, workout_id="old", started_at=started, duration=25, vendor_type_id=16,
        ))
        snapshots = []
        for offset in offsets:
            target = day + timedelta(days=offset)
            run = orm.AnalysisRun(
                id=f"catalog-history-{offset}", user_id=user, target_date=target, status="SUCCEEDED",
                started_at=started, completed_at=started,
                intelligence_version=INTELLIGENCE_VERSION,
                decision_policy_version=DECISION_POLICY_VERSION, evidence_version=EVIDENCE_VERSION,
                input_revision_used=repo.analysis_input_revision(user),
            )
            db.add(run)
            snapshot = repo.save_analysis_snapshot(
                run.id, user, "daily", target, target, DAILY_SCHEMA_VERSION,
                INTELLIGENCE_VERSION, DECISION_POLICY_VERSION, EVIDENCE_VERSION,
                {"synthetic": True, "original_offset": offset},
            )
            snapshots.append((snapshot.id, target, deepcopy(snapshot.payload)))
        assert repo.save_workout_classification(user, "old", {
            "type": "other", "sport_mode": "free_training",
            "sport_mode_label": "自由训练", "training_family": "mixed",
        })
        jobs = [job for job in repo.analysis_jobs(user) if job.event_type == "catalog_refresh"]
        assert {job.target_date for job in jobs} == {day + timedelta(days=n) for n in (0, 10, 179)}
        assert all(job.delivery_period is None for job in jobs)
        assert all(job.affected_start == day and job.affected_end == day for job in jobs)
        for snapshot_id, target, payload in snapshots:
            assert db.get(orm.AnalysisSnapshot, snapshot_id).payload == payload
            last_good = repo.latest_good_analysis_snapshot(user, "daily", target)
            assert last_good is not None and last_good.id == snapshot_id
