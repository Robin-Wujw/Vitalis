"""Synthetic API and transactional contracts for explicit chart inputs."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from vitalis.adapters.persistence.database import init_db
from vitalis.adapters.persistence.models import AnalysisJob, NotificationDelivery, User, Workout
from vitalis.adapters.persistence.repositories import HealthRepository
from vitalis.adapters.persistence.chart_inputs import (
    ChartAnnotation, ChartExternalSet, ChartExternalWorkout, ChartInputRevision,
    ChartInputWriteRequest, ChartWorkoutReview, SqlChartInputStore,
)
from vitalis.application.chart_inputs import (
    ActivityReviewInput, AnnotationInput, AnnotationPatch, ChartInputConflict,
    ChartInputLimitError, ChartInputNotFound, ChartInputService, ChartInputValidationError,
    DeleteAnnotationInput, HevyImportInput, HevyLinkInput, parse_hevy_csv,
)


DAY = date(2026, 9, 20)
NOW = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
START = datetime(2026, 9, 20, 10, tzinfo=timezone.utc)
CSV = (
    'title,start_time,end_time,exercise_title,set_index,set_type,weight_kg,reps,rpe\n'
    'Synthetic Session,2026-09-20T10:00:00Z,2026-09-20T11:00:00Z,Unknown press variant,0,warmup,10,12,\n'
    'Synthetic Session,2026-09-20T10:00:00Z,2026-09-20T11:00:00Z,Unknown press variant,1,normal,20,8,8\n'
)


@pytest.fixture
def chart_db(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'chart-inputs.db').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        db.add_all([User(id="chart-owner"), User(id="chart-other")])
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture
def chart_service(chart_db):
    clock = {"now": NOW}
    service = ChartInputService(
        SqlChartInputStore(chart_db, timezone_name="UTC"),
        today_factory=lambda: DAY + timedelta(days=1), now_factory=lambda: clock["now"],
        timezone_name="UTC",
    )
    return service, clock


def _annotation(**changes):
    return AnnotationInput.model_validate({
        "confirmed": True, "started_at": START, "ended_at": START + timedelta(hours=1),
        "text": "Synthetic context only", "tag": "travel", **changes,
    })


def _workout(factory, workout_id="zepp-one", *, user="chart-owner", start=START, **data):
    payload = {
        "type": "strength", "sport_mode": "strength_training", "training_family": "strength",
        "auto_recognized": False, "recognition_confidence": "HIGH",
        "started_at": start.isoformat(), "ended_at": (start + timedelta(hours=1)).isoformat(),
        "duration": 60, **data,
    }
    with factory.begin() as db:
        db.add(Workout(user_id=user, source="zepp", workout_id=workout_id,
                       started_at=start.replace(tzinfo=None), data=payload))
    return payload


def _count(factory, model):
    with factory() as db:
        return db.scalar(select(func.count()).select_from(model))


def test_annotation_lifecycle_revision_history_and_read_only_snapshot(chart_db, chart_service):
    service, clock = chart_service
    result = service.create_annotation("chart-owner", _annotation(), idempotency_key="annotation-create-001")
    annotation = result["annotation"]
    assert annotation["revision"] == 1
    assert annotation["qualification"] == "user_annotation"
    assert annotation["time_basis"] == "calendar_day"
    assert result["input_event"]["affected_dates"] == [DAY.isoformat()]
    assert result["input_event"]["analysis_job_ids"]
    assert service.create_annotation("chart-owner", _annotation(), idempotency_key="annotation-create-001") == result
    with pytest.raises(ChartInputConflict):
        service.create_annotation("chart-owner", _annotation(text="Different synthetic text"), idempotency_key="annotation-create-001")
    before = _count(chart_db, AnalysisJob)
    listed = service.annotations("chart-owner", DAY, DAY)
    assert listed["items"][0]["id"] == annotation["id"]
    assert service.annotations("chart-other", DAY, DAY)["items"] == []
    assert _count(chart_db, AnalysisJob) == before
    clock["now"] += timedelta(minutes=1)
    patched = service.patch_annotation(
        "chart-owner", annotation["id"], AnnotationPatch(confirmed=True, expected_revision=1, text="Revised synthetic note", tag=None),
        idempotency_key="annotation-patch-001",
    )
    assert patched["annotation"]["revision"] == 2
    assert patched["annotation"]["tag"] is None
    with pytest.raises(ChartInputConflict):
        service.patch_annotation("chart-owner", annotation["id"], AnnotationPatch(confirmed=True, expected_revision=1, text="Stale"), idempotency_key="annotation-stale-001")
    with pytest.raises(ChartInputNotFound):
        service.delete_annotation("chart-other", annotation["id"], DeleteAnnotationInput(confirmed=True, expected_revision=2), idempotency_key="annotation-other-001")
    clock["now"] += timedelta(minutes=1)
    deleted = service.delete_annotation("chart-owner", annotation["id"], DeleteAnnotationInput(confirmed=True, expected_revision=2), idempotency_key="annotation-delete-001")
    assert deleted["annotation"]["deleted"] is True
    assert service.annotations("chart-owner", DAY, DAY)["items"] == []
    assert service.snapshot("chart-owner", DAY, DAY, as_of=NOW)["annotations"][0]["text"] == "Synthetic context only"
    assert service.snapshot("chart-owner", DAY, DAY, as_of=clock["now"])["annotations"] == []
    assert _count(chart_db, ChartInputRevision) == 3
    assert _count(chart_db, NotificationDelivery) == 0
    with chart_db() as db:
        assert all(job.execution_context.get("notification_policy") == "suppress" for job in db.scalars(select(AnalysisJob)))


def test_annotation_cross_day_edit_invalidates_old_and_new_dates(chart_service):
    service, _ = chart_service
    result = service.create_annotation(
        "chart-owner", _annotation(started_at=START - timedelta(days=2), ended_at=START - timedelta(days=1)),
        idempotency_key="annotation-span-001",
    )
    changed = service.patch_annotation(
        "chart-owner", result["annotation"]["id"],
        AnnotationPatch(confirmed=True, expected_revision=1, started_at=START, ended_at=START + timedelta(hours=1)),
        idempotency_key="annotation-span-002",
    )
    assert changed["input_event"]["affected_dates"] == ["2026-09-18", "2026-09-19", "2026-09-20"]


def test_input_and_idempotency_rollback_if_enqueue_fails(chart_db, chart_service, monkeypatch):
    service, _ = chart_service
    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic queue failure")
    monkeypatch.setattr(HealthRepository, "enqueue_input_change", fail)
    with pytest.raises(RuntimeError, match="synthetic queue"):
        service.create_annotation("chart-owner", _annotation(), idempotency_key="annotation-rollback-001")
    assert _count(chart_db, ChartAnnotation) == 0
    assert _count(chart_db, ChartInputWriteRequest) == 0
    assert _count(chart_db, ChartInputRevision) == 0
    with chart_db() as db:
        assert db.get(User, "chart-owner").analysis_input_revision == 0


def test_concurrent_same_key_creates_one_annotation(chart_db, chart_service):
    service, _ = chart_service
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.create_annotation("chart-owner", _annotation(), idempotency_key="annotation-concurrent-001"), range(2)))
    assert results[0] == results[1]
    assert _count(chart_db, ChartAnnotation) == 1
    assert _count(chart_db, ChartInputRevision) == 1


def test_review_queue_only_eligible_records_preserves_vendor_and_decision_history(chart_db, chart_service):
    service, clock = chart_service
    original = _workout(chart_db, "auto", auto_recognized=True)
    _workout(chart_db, "unknown", sport_mode="unknown", recognition_confidence="NONE")
    _workout(chart_db, "manual")
    _workout(chart_db, "foreign", user="chart-other", auto_recognized=True)
    queue = service.activity_reviews("chart-owner", DAY, DAY)
    assert {item["workout_id"] for item in queue["items"]} == {"auto", "unknown"}
    assert _count(chart_db, AnalysisJob) == 0
    body = ActivityReviewInput(confirmed=True, workout_source="zepp", workout_id="auto", expected_revision=0, action="classify", classification="walking")
    classified = service.review_activity("chart-owner", body, idempotency_key="review-classify-001")
    assert classified["review"]["classification"] == "walking"
    assert classified["review"]["applied_to_source"] is False
    assert classified["review"]["original"]["type"] == "strength"
    assert service.review_activity("chart-owner", body, idempotency_key="review-classify-001") == classified
    assert [item["workout_id"] for item in service.activity_reviews("chart-owner", DAY, DAY)["items"]] == ["unknown"]
    clock["now"] += timedelta(minutes=1)
    ignored = service.review_activity("chart-owner", body.model_copy(update={"action": "ignore", "classification": None, "expected_revision": 1}), idempotency_key="review-ignore-001")
    assert ignored["review"]["decision"] == "ignored"
    clock["now"] += timedelta(minutes=1)
    restored = service.review_activity("chart-owner", body.model_copy(update={"action": "restore", "classification": None, "expected_revision": 2}), idempotency_key="review-restore-001")
    assert restored["review"]["decision"] == "pending"
    assert restored["review"]["revision"] == 3
    assert {item["workout_id"] for item in service.activity_reviews("chart-owner", DAY, DAY)["items"]} == {"auto", "unknown"}
    assert service.snapshot("chart-owner", DAY, DAY, as_of=NOW)["workout_reviews"][0]["classification"] == "walking"
    with chart_db() as db:
        assert db.scalar(select(Workout).where(Workout.workout_id == "auto")).data == original
    with pytest.raises(ChartInputConflict):
        service.review_activity("chart-owner", body, idempotency_key="review-stale-001")
    with pytest.raises(ChartInputNotFound):
        service.review_activity("chart-other", body, idempotency_key="review-foreign-001")
    with pytest.raises(ChartInputValidationError):
        service.review_activity("chart-owner", body.model_copy(update={"workout_id": "manual"}), idempotency_key="review-manual-001")


def test_hevy_import_preserves_sets_provenance_and_deduplicates(chart_db, chart_service):
    service, _ = chart_service
    body = HevyImportInput(confirmed=True, csv=CSV, timezone="UTC")
    imported = service.import_hevy("chart-owner", body, idempotency_key="hevy-import-00100")
    assert imported["created_count"] == 1
    workout = imported["external_workouts"][0]
    assert workout["source"] == "hevy"
    assert workout["source_scope"] == "hevy_csv"
    assert workout["qualification"] == "external_user_upload"
    assert workout["linked_workout_id"] is None
    assert workout["unmapped_set_count"] == 2
    assert [item["reps"] for item in workout["sets"]] == [12, 8]
    assert [item["set_type"] for item in workout["sets"]] == ["warmup", "normal"]
    assert workout["sets"][1]["rpe"] == 8
    assert workout["sets"][1]["weight_value"] == 20
    assert workout["sets"][1]["weight_unit"] == "kg"
    assert all(item["muscle_groups"] == [] for item in workout["sets"])
    assert service.import_hevy("chart-owner", body, idempotency_key="hevy-import-00100") == imported
    duplicate = service.import_hevy("chart-owner", body, idempotency_key="hevy-import-00200")
    assert duplicate["created_count"] == 0
    assert duplicate["input_event"] is None
    assert _count(chart_db, ChartExternalWorkout) == 1
    assert _count(chart_db, ChartExternalSet) == 2
    assert _count(chart_db, Workout) == 0
    assert service.external_workouts("chart-other", DAY, DAY)["items"] == []
    with pytest.raises(ChartInputConflict):
        service.import_hevy("chart-owner", HevyImportInput(confirmed=True, csv=CSV.replace(",20,8,8", ",21,8,8"), timezone="UTC"), idempotency_key="hevy-modified-0010")


def test_hevy_candidates_never_link_automatically_ambiguous_then_explicit_single_count(chart_db, chart_service):
    service, clock = chart_service
    _workout(chart_db, "overlap")
    _workout(chart_db, "same-day", start=START + timedelta(hours=4))
    _workout(chart_db, "foreign", user="chart-other")
    imported = service.import_hevy("chart-owner", HevyImportInput(confirmed=True, csv=CSV), idempotency_key="hevy-candidate-001")
    external = imported["external_workouts"][0]
    candidates = service.hevy_candidates("chart-owner", external["id"])
    assert candidates["state"] == "ambiguous"
    assert {item["workout_id"] for item in candidates["candidates"]} == {"overlap", "same-day"}
    assert {item["match_basis"] for item in candidates["candidates"]} == {"time_overlap", "same_activity_day"}
    assert service.external_workout("chart-owner", external["id"])["linked_workout_id"] is None
    with pytest.raises(ChartInputNotFound):
        service.link_hevy("chart-owner", external["id"], HevyLinkInput(confirmed=True, expected_revision=1, workout_id="foreign"), idempotency_key="hevy-cross-user-001")
    clock["now"] += timedelta(minutes=1)
    body = HevyLinkInput(confirmed=True, expected_revision=1, workout_id="overlap")
    linked = service.link_hevy("chart-owner", external["id"], body, idempotency_key="hevy-link-001000")
    assert linked["external_workout"]["revision"] == 2
    assert linked["external_workout"]["source"] == "hevy"
    assert linked["external_workout"]["user_decision"] == "linked"
    assert linked["external_workout"]["analysis_identity"] == {"source": "zepp", "workout_id": "overlap"}
    assert linked["external_workout"]["count_as_separate_workout"] is False
    assert service.link_hevy("chart-owner", external["id"], body, idempotency_key="hevy-link-001000") == linked
    assert _count(chart_db, Workout) == 3
    assert service.snapshot("chart-owner", DAY, DAY, as_of=NOW)["external_workouts"][0]["linked_workout_id"] is None
    assert service.snapshot("chart-owner", DAY, DAY, as_of=clock["now"])["external_workouts"][0]["linked_workout_id"] == "overlap"
    with pytest.raises(ChartInputConflict):
        service.link_hevy("chart-owner", external["id"], body, idempotency_key="hevy-link-stale-001")


def test_hevy_second_external_session_cannot_duplicate_link(chart_db, chart_service):
    service, _ = chart_service
    _workout(chart_db)
    one = service.import_hevy("chart-owner", HevyImportInput(confirmed=True, csv=CSV), idempotency_key="hevy-link-first-001")["external_workouts"][0]
    two = service.import_hevy("chart-owner", HevyImportInput(confirmed=True, csv=CSV.replace("Synthetic Session", "Second Synthetic Session")), idempotency_key="hevy-link-second-001")["external_workouts"][0]
    service.link_hevy("chart-owner", one["id"], HevyLinkInput(confirmed=True, expected_revision=1, workout_id="zepp-one"), idempotency_key="hevy-link-first-002")
    with pytest.raises(ChartInputConflict):
        service.link_hevy("chart-owner", two["id"], HevyLinkInput(confirmed=True, expected_revision=1, workout_id="zepp-one"), idempotency_key="hevy-link-second-002")


def test_hevy_native_local_dates_and_pounds_keep_explicit_units():
    text = 'title,start_time,end_time,exercise_title,set_index,set_type,weight_lbs,reps,rpe\n"Synthetic","20 Sep 2026, 18:00","20 Sep 2026, 19:00",Synthetic lift,0,normal,45,6,7\n'
    parsed = parse_hevy_csv(HevyImportInput(confirmed=True, csv=text, timezone="Asia/Shanghai"), timezone_name="UTC")
    assert parsed[0].started_at == START
    assert parsed[0].sets[0].weight_unit == "lb"
    assert parsed[0].sets[0].weight_value == 45
    assert parsed[0].sets[0].exercise_id is None


@pytest.mark.parametrize("text", [
    "title,start_time\nx,not-a-time\n", CSV.replace(",20,8,8", ",nan,8,8"),
    CSV.replace(",20,8,8", ",20,8,11"), CSV.replace("11:00:00Z", "09:00:00Z"),
    CSV.replace("exercise_title", "unsupported_exercise_header"),
])
def test_hevy_invalid_csv_is_rejected_atomically(chart_db, chart_service, text):
    service, _ = chart_service
    with pytest.raises(ChartInputValidationError):
        service.import_hevy("chart-owner", HevyImportInput(confirmed=True, csv=text), idempotency_key="hevy-invalid-00100")
    assert _count(chart_db, ChartExternalWorkout) == 0
    assert _count(chart_db, ChartInputWriteRequest) == 0


def test_csv_budget_and_annotation_validation():
    with pytest.raises((ValidationError, ChartInputLimitError)):
        parse_hevy_csv(HevyImportInput(confirmed=True, csv="x" * 1_048_577), timezone_name="UTC")
    too_many = CSV.splitlines()[0] + "\n" + (CSV.splitlines()[1] + "\n") * 2001
    with pytest.raises(ChartInputLimitError):
        parse_hevy_csv(HevyImportInput(confirmed=True, csv=too_many), timezone_name="UTC")
    for changes in ({"confirmed": False}, {"text": "  "}, {"started_at": START.replace(tzinfo=None)}, {"ended_at": START - timedelta(seconds=1)}):
        with pytest.raises(ValidationError):
            _annotation(**changes)


def test_api_read_feedback_scope_errors_and_delete_body(chart_service, issue_token):
    from tests.conftest import ASGITestClient
    from vitalis.entrypoints.api.app import create_app
    from vitalis.entrypoints.api.routes.chart_inputs import chart_input_service, router

    service, _ = chart_service
    app = create_app()
    if "/api/chart-inputs/annotations" not in app.openapi()["paths"]:
        app.include_router(router, prefix="/api")
    app.dependency_overrides[chart_input_service] = lambda: service
    client = ASGITestClient(app)
    read = {"Authorization": f"Bearer {issue_token('chart-owner', ['read'])}"}
    write = {"Authorization": f"Bearer {issue_token('chart-owner', ['feedback'])}", "Idempotency-Key": "api-annotation-001"}
    url = "/api/chart-inputs/annotations"
    assert client.get(url, headers=read).status_code == 200
    assert client.get(url, headers=write).status_code == 403
    assert client.post(url, headers=read, json=_annotation().model_dump(mode="json")).status_code == 403
    assert client.post(url, headers={"Authorization": write["Authorization"]}, json=_annotation().model_dump(mode="json")).status_code == 422
    result = client.post(url, headers=write, json=_annotation().model_dump(mode="json"))
    assert result.status_code == 201
    annotation_id = result.json()["annotation"]["id"]
    conflict = client.request("PATCH", f"{url}/{annotation_id}", headers={**write, "Idempotency-Key": "api-annotation-002"}, json={"confirmed": True, "expected_revision": 9, "text": "Synthetic stale"})
    assert conflict.status_code == 409
    assert conflict.json()["state"] == "failed"
    assert conflict.json()["failure_code"] == "conflict"
    assert conflict.json()["retryable"] is False
    assert conflict.json()["next_action"] == "refresh_and_retry"
    deleted = client.request("DELETE", f"{url}/{annotation_id}", headers={**write, "Idempotency-Key": "api-annotation-003"}, json={"confirmed": True, "expected_revision": 1})
    assert deleted.status_code == 200
    assert deleted.json()["annotation"]["deleted"] is True


def test_activity_rules_and_hevy_suggestions_are_read_only_explicit_and_conflicting(chart_db, chart_service):
    from vitalis.application.chart_inputs import ActivityRuleInput
    service, _ = chart_service
    _workout(chart_db, "auto", auto_recognized=True, title="Synthetic walk", duration=30)
    service.import_hevy("chart-owner", HevyImportInput(confirmed=True, csv=CSV), idempotency_key="suggestion-hevy-001")
    rule = ActivityRuleInput(confirmed=True, name="Synthetic walk rule", classification="walking", conditions={"title_equals": "Synthetic walk", "min_duration_minutes": 20, "max_duration_minutes": 40, "weekdays": [7]})
    saved = service.save_activity_rule("chart-owner", rule, idempotency_key="activity-rule-001")
    assert saved["rule"]["mode"] == "suggest_only"
    before = _count(chart_db, AnalysisJob), _count(chart_db, ChartInputWriteRequest)
    item = service.activity_reviews("chart-owner", DAY, DAY)["items"][0]
    assert item["decision"] == "pending"
    assert item["revision"] == 0
    assert item["suggestion_state"] == "conflicting"
    assert {suggestion["classification"] for suggestion in item["suggestions"]} == {"walking", "strength"}
    assert {suggestion["source"] for suggestion in item["suggestions"]} == {"user_rule", "hevy_csv"}
    assert (_count(chart_db, AnalysisJob), _count(chart_db, ChartInputWriteRequest)) == before
    assert service.activity_rules("chart-other")["items"] == []
    assert _count(chart_db, ChartWorkoutReview) == 0
    disabled = ActivityRuleInput(**{**rule.model_dump(), "rule_id": saved["rule"]["id"], "expected_revision": 1, "enabled": False})
    service.save_activity_rule("chart-owner", disabled, idempotency_key="activity-rule-002")
    assert service.activity_reviews("chart-owner", DAY, DAY)["items"][0]["suggested_classification"] == "strength"
    with pytest.raises(ChartInputConflict):
        service.save_activity_rule("chart-owner", disabled, idempotency_key="activity-rule-stale")
    with pytest.raises(ValidationError):
        ActivityRuleInput(confirmed=True, name="Bad", classification="strength", conditions={"name_contains": "press"})


def test_explicit_hevy_mapping_preserves_external_source_and_history(chart_db, chart_service):
    from vitalis.application.chart_inputs import HevySetMappingInput
    service, clock = chart_service
    external = service.import_hevy("chart-owner", HevyImportInput(confirmed=True, csv=CSV), idempotency_key="mapping-hevy-00100")["external_workouts"][0]
    clock["now"] += timedelta(minutes=1)
    body = HevySetMappingInput(confirmed=True, expected_revision=1, mappings=[{"set_id": external["sets"][1]["id"], "exercise_id": "user:synthetic-lift", "primary_muscles": ["chest"], "secondary_muscles": ["triceps"], "side": "bilateral"}])
    result = service.map_hevy_sets("chart-owner", external["id"], body, idempotency_key="mapping-hevy-00200")
    item = result["external_workout"]["sets"][1]
    assert item["source"] == "hevy"
    assert item["mapping_source"] == "user_confirmed"
    assert item["primary_muscles"] == ["chest"]
    assert item["secondary_muscles"] == ["triceps"]
    assert item["weight_value"] == 20
    assert result["external_workout"]["unmapped_set_count"] == 1
    assert result["external_workout"]["revision"] == 2
    assert service.map_hevy_sets("chart-owner", external["id"], body, idempotency_key="mapping-hevy-00200") == result
    assert service.snapshot("chart-owner", DAY, DAY, as_of=NOW)["external_workouts"][0]["sets"][1]["muscle_groups"] == []
    with pytest.raises(ChartInputConflict):
        service.map_hevy_sets("chart-owner", external["id"], body, idempotency_key="mapping-hevy-stale")
    with pytest.raises(ChartInputNotFound):
        service.map_hevy_sets("chart-other", external["id"], body, idempotency_key="mapping-hevy-other")
    with pytest.raises(ChartInputNotFound):
        service.map_hevy_sets("chart-owner", external["id"], body.model_copy(update={"expected_revision": 2, "mappings": [body.mappings[0].model_copy(update={"set_id": "foreign-set"})]}), idempotency_key="mapping-hevy-unknown")
    with pytest.raises(ValidationError):
        HevySetMappingInput(confirmed=True, expected_revision=2, mappings=[{"set_id": "set", "primary_muscles": ["invented-muscle"]}])


def test_snapshot_reader_uses_callers_transaction(chart_db, chart_service):
    from vitalis.adapters.persistence.chart_inputs import read_chart_input_snapshot
    service, _ = chart_service
    service.create_annotation("chart-owner", _annotation(), idempotency_key="in-transaction-001")
    with chart_db() as db:
        owner = db.get(User, "chart-owner")
        owner.analysis_input_revision = 900
        db.flush()
        result = read_chart_input_snapshot(db, "chart-owner", DAY, DAY, as_of=NOW)
        assert result["annotations"]
        assert owner.analysis_input_revision == 900
        db.rollback()
    with chart_db() as db:
        assert db.get(User, "chart-owner").analysis_input_revision == 1


def test_input_event_reference_resolves_to_transactional_audit(chart_db, chart_service):
    from vitalis.adapters.persistence.input_events import InputEvent
    service, _ = chart_service
    result = service.create_annotation("chart-owner", _annotation(), idempotency_key="audit-reference-001")
    with chart_db() as db:
        event = db.get(InputEvent, result["input_event"]["id"])
        assert event is not None
        assert event.user_id == "chart-owner"
        assert event.input_revision == result["input_event"]["input_revision"]


def test_extended_api_operations_are_scoped_and_do_not_write_on_get(chart_db, chart_service, issue_token):
    from tests.conftest import ASGITestClient
    from vitalis.entrypoints.api.app import create_app
    from vitalis.entrypoints.api.routes.chart_inputs import chart_input_service
    service, _ = chart_service
    _workout(chart_db, auto_recognized=True)
    app = create_app()
    app.dependency_overrides[chart_input_service] = lambda: service
    client = ASGITestClient(app)
    read = {"Authorization": f"Bearer {issue_token('chart-owner', ['read'])}"}
    write = {"Authorization": f"Bearer {issue_token('chart-owner', ['feedback'])}", "Idempotency-Key": "extended-api-00100"}
    prefix = "/api/chart-inputs"
    body = {"confirmed": True, "csv": CSV, "timezone": "UTC"}
    assert client.post(f"{prefix}/hevy-imports", headers=read, json=body).status_code == 403
    uploaded = client.post(f"{prefix}/hevy-imports", headers=write, json=body)
    assert uploaded.status_code == 201
    external = uploaded.json()["external_workouts"][0]
    rule_body = {"confirmed": True, "name": "Synthetic", "classification": "walking", "conditions": {"min_duration_minutes": 10}}
    rule = client.post(f"{prefix}/activity-rules", headers={**write, "Idempotency-Key": "extended-rule-001"}, json=rule_body)
    assert rule.status_code == 200
    patch = {"confirmed": True, "expected_revision": 1, "mappings": [{"set_id": external["sets"][0]["id"], "primary_muscles": ["chest"]}]}
    assert client.request("PATCH", f"{prefix}/external-workouts/{external['id']}/sets", headers=read, json=patch).status_code == 403
    assert client.request("PATCH", f"{prefix}/external-workouts/{external['id']}/sets", headers={**write, "Idempotency-Key": "extended-map-0010"}, json=patch).status_code == 200
    before = _count(chart_db, ChartInputWriteRequest), _count(chart_db, AnalysisJob)
    for path in ("activity-rules", "activity-reviews", "external-workouts", f"external-workouts/{external['id']}", f"external-workouts/{external['id']}/candidates", f"snapshot?start={DAY}&end={DAY}"):
        assert client.get(f"{prefix}/{path}", headers=read).status_code == 200
    assert (_count(chart_db, ChartInputWriteRequest), _count(chart_db, AnalysisJob)) == before
    naive = client.get(f"{prefix}/snapshot?start={DAY}&end={DAY}&as_of=2026-09-21T12:00:00", headers=read)
    assert naive.status_code == 422
    assert naive.json()["failure_code"] == "validation_error"


def test_dst_and_multibyte_upload_limits_reject_without_invented_time():
    for local in ("2026-03-08T02:15:00", "2026-11-01T01:15:00"):
        ambiguous = CSV.replace("2026-09-20T10:00:00Z", local).replace("2026-09-20T11:00:00Z", local)
        with pytest.raises(ChartInputValidationError):
            parse_hevy_csv(HevyImportInput(confirmed=True, csv=ambiguous, timezone="America/New_York"), timezone_name="UTC")
    with pytest.raises(ChartInputLimitError):
        parse_hevy_csv(HevyImportInput(confirmed=True, csv="合" * 400_000), timezone_name="UTC")


def test_user_delete_cascades_input_rows(chart_db, chart_service):
    service, _ = chart_service
    service.create_annotation("chart-owner", _annotation(), idempotency_key="cascade-annotation-001")
    service.import_hevy("chart-owner", HevyImportInput(confirmed=True, csv=CSV), idempotency_key="cascade-hevy-00100")
    with chart_db.begin() as db:
        db.delete(db.get(User, "chart-owner"))
    for model in (ChartAnnotation, ChartExternalWorkout, ChartExternalSet, ChartInputRevision, ChartInputWriteRequest, ChartWorkoutReview):
        assert _count(chart_db, model) == 0
