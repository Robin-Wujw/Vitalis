from datetime import date
import json

from vitalis.intelligence.contracts import (
    ConfidenceBand,
    Sex,
    UserProfilePatch,
)
from vitalis.intelligence.service import IntelligenceAction, IntelligenceCommand, IntelligenceQuery
from vitalis.storage import HealthRepository, session_scope
from vitalis.storage.models import AnalysisRun, UserProfile, UserProfileRevision


TARGET = date(2026, 8, 28)


def test_empty_profile_read_is_typed_and_does_not_write():
    user_id = "profile-empty-user"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        profile = repo.user_profile(user_id)
        assert profile.user_id == user_id
        assert profile.revision == 0
        assert db.get(UserProfile, user_id) is None


def test_profile_patch_is_revisioned_and_explicit_null_clears():
    user_id = "profile-revision-user"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)

    first = IntelligenceAction().patch_profile(
        user_id,
        UserProfilePatch(
            expected_revision=0,
            sex=Sex.MALE,
            confirmed_hrmax_bpm=190,
            sleep_target_minutes=480,
        ),
    )
    assert first.revision == 1
    assert first.sex.value == Sex.MALE
    assert first.sex.source.value == "USER_CONFIRMED"
    assert first.sex.confidence == ConfidenceBand.HIGH

    cleared = IntelligenceAction().patch_profile(
        user_id,
        UserProfilePatch(expected_revision=1, sex=None),
    )
    assert cleared.revision == 2
    assert cleared.sex is None
    assert cleared.confirmed_hrmax_bpm.value == 190
    with session_scope() as db:
        revisions = db.query(UserProfileRevision).filter_by(user_id=user_id).order_by(UserProfileRevision.revision).all()
        assert [row.revision for row in revisions] == [1, 2]
        assert revisions[1].changed_fields == ["sex"]


def test_profile_patch_conflict_and_user_isolation():
    owner = "profile-owner"
    other = "profile-other"
    with session_scope() as db:
        repo = HealthRepository(db)
        for user_id in (owner, other):
            repo.delete_for_user(user_id)
            repo.upsert_user(user_id)
    IntelligenceAction().patch_profile(
        owner,
        UserProfilePatch(expected_revision=0, confirmed_hrmax_bpm=190),
    )

    try:
        IntelligenceAction().patch_profile(
            owner,
            UserProfilePatch(expected_revision=0, sleep_target_minutes=480),
        )
    except ValueError as exc:
        assert "版本冲突" in str(exc)
    else:
        raise AssertionError("expected revision conflict")
    assert IntelligenceQuery().profile(other).revision == 0
    assert IntelligenceQuery().profile(other).confirmed_hrmax_bpm is None


def test_profile_api_context_missing_inputs_and_analysis_revision(client):
    user_id = "profile-api-user"
    headers = {"X-User-Id": user_id}
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)

    empty = client.get("/api/v1/intelligence/profile", headers=headers)
    assert empty.status_code == 200
    assert empty.json()["revision"] == 0
    with session_scope() as db:
        assert db.query(UserProfile).filter_by(user_id=user_id).count() == 0

    analyzed = client.post("/api/v1/intelligence/analyze", headers=headers)
    assert analyzed.status_code == 201
    assert analyzed.json()["run"]["profile_revision_used"] == 0
    context = client.get("/api/v1/intelligence/context", headers=headers)
    assert context.status_code == 200
    body = context.json()
    assert body["schema_version"] == "6.0"
    assert body["recent"]["training_duration_minutes"] is None
    assert {item["field"] for item in body["missing_inputs"]} == {
        "sex", "confirmed_hrmax_bpm", "sleep_target_minutes"
    }
    assert len(json.dumps(body, ensure_ascii=False).encode()) < 20_000

    patched = client.request(
        "PATCH",
        "/api/v1/intelligence/profile",
        headers=headers,
        json={
            "expected_revision": 0,
            "sex": "MALE",
            "confirmed_hrmax_bpm": 190,
            "sleep_target_minutes": 480,
        },
    )
    assert patched.status_code == 200
    assert patched.json()["revision"] == 1
    conflict = client.request(
        "PATCH",
        "/api/v1/intelligence/profile",
        headers=headers,
        json={"expected_revision": 0, "sex": "FEMALE"},
    )
    assert conflict.status_code == 409

    context = client.get("/api/v1/intelligence/context", headers=headers).json()
    assert context["missing_inputs"] == []
    rerun = client.post("/api/v1/intelligence/analyze", headers=headers)
    assert rerun.status_code == 201
    assert rerun.json()["run"]["profile_revision_used"] == 1
    with session_scope() as db:
        rows = db.query(AnalysisRun).filter_by(user_id=user_id).order_by(AnalysisRun.started_at).all()
        assert rows[-1].profile_revision_used == 1


def test_shadow_open_health_failure_does_not_abort_core_analysis(monkeypatch):
    user_id = "profile-shadow-failure"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
    IntelligenceAction().patch_profile(
        user_id,
        UserProfilePatch(
            expected_revision=0,
            sex="MALE",
            confirmed_hrmax_bpm=190,
        ),
    )

    def fail_shadow_loader(*_args, **_kwargs):
        raise RuntimeError("shadow input failure")

    monkeypatch.setattr(
        HealthRepository, "open_health_load_inputs", fail_shadow_loader
    )
    result = IntelligenceCommand().analyze(user_id, TARGET)

    assert result.run.status.value == "SUCCEEDED"
    assert result.daily.open_health_insights is None
    assert result.daily.metadata["open_health_status"] == "REFUSED_INTERNAL_ERROR"
    assert result.daily.decision.action is not None


def test_open_health_profile_inputs_do_not_change_decision_state_or_plan():
    user_id = "profile-decision-invariance"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)

    before = IntelligenceCommand().analyze(user_id, TARGET).daily.decision
    IntelligenceAction().patch_profile(
        user_id,
        UserProfilePatch(expected_revision=0, sex="MALE", confirmed_hrmax_bpm=190),
    )
    after_result = IntelligenceCommand().analyze(user_id, TARGET)
    after = after_result.daily.decision

    assert before.action == after.action
    assert before.confidence == after.confidence
    assert before.rule_ids == after.rule_ids
    assert before.action_plan == after.action_plan
    assert after_result.open_health_insights is not None
    assert after_result.open_health_insights.training_load is not None
