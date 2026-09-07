from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import pytest

from vitalis.intelligence.contracts import MorningBriefing

from vitalis.intelligence.morning_briefing import MorningBriefingEngine
from vitalis.services import daily_push
from vitalis.services.push_service import PushService
from tests.test_report_content import synthetic_daily_fixture


TARGET_DATE = date(2026, 8, 29)


def _daily(*, prior_7d_verified=False):
    payload = synthetic_daily_fixture("complete")
    payload["date"] = TARGET_DATE.isoformat()
    payload["report_context"]["target_date"] = TARGET_DATE.isoformat()
    payload["report_context"]["training_history"] = {
        "status": "PARTIAL" if not prior_7d_verified else "COMPLETE",
        "verified_days": [],
        "prior_7d_verified": prior_7d_verified,
    }
    return payload


def test_facts_only_projection_is_a_strict_sleep_body_whitelist():
    payload = _daily()
    marker = "UNSAFE-TRAINING-PRESCRIPTION"
    payload["summary"] = [marker]
    payload["sections"] = [{"title": marker}]
    payload["coach"] = marker
    payload["id"] = marker
    payload["decision"]["action_plan"]["primary_session"]["title"] = marker
    payload["decision"]["action_plan"]["primary_session"]["steps"] = [{"name": marker}]
    original = deepcopy(payload)

    briefing = MorningBriefingEngine().build_payload(
        payload,
        {
            "facts_only": True,
            "coverage_reason": "prior_7d_unverified",
        },
    )

    assert payload == original
    assert "action_plan" not in briefing
    assert "primary_session" not in repr(briefing)
    assert marker not in repr(briefing)
    assert [section["key"] for section in briefing["sections"]] == ["sleep", "recovery"]
    assert briefing["report_context"]["delivery_metadata"] == {
        "facts_only": True,
        "coverage_reason": "prior_7d_unverified",
    }
    assert any("训练历史覆盖尚未核验" in item for item in briefing["cautions"])


def test_facts_only_push_renders_sleep_body_and_no_training_sections():
    received = []
    service = PushService(pushplus_token="")
    service.add_handler(received.append)
    service.push_daily_profile(
        "synthetic-user",
        {
            **_daily(),
            "delivery_metadata": {
                "facts_only": True,
                "coverage_reason": "training_history_missing",
            },
        },
        period="morning",
    )

    message = received[0]
    assert message.title.endswith("事实版")
    assert "昨晚睡眠" in message.body
    assert "今早恢复信号" in message.body
    assert "今天的安排" not in message.body
    assert "训练历史覆盖尚未核验" in message.body
    assert "action_plan" not in repr(message.extras)
    assert "primary_session" not in repr(message.extras)


def test_unverified_history_sends_once_and_marks_scheduled_delivery(monkeypatch, tmp_path):
    monkeypatch.setattr(daily_push, "local_today", lambda: TARGET_DATE)
    payload = _daily()
    sent = []

    class Service:
        def __init__(self, pushplus_token):
            pass

        def push_daily_profile(self, user_id, profile, period):
            sent.append((user_id, profile, period))
            return {"_pushplus_handler": "ok"}

    monkeypatch.setattr(daily_push, "PushService", Service)
    kwargs = {
        "user_id": "synthetic-user",
        "pushplus_token": "private-token",
        "daily": payload,
        "period": "morning",
        "target_date": TARGET_DATE,
        "state_dir": tmp_path,
    }

    result = daily_push.deliver_daily_report(**kwargs)
    second = daily_push.deliver_daily_report(**kwargs)

    assert result["status"] == "sent"
    assert result["mode"] == "facts_only"
    assert result["facts_only"] is True
    assert result["coverage_reason"] == "prior_7d_unverified"
    assert len(sent) == 1
    assert sent[0][1]["delivery_metadata"] == {
        "facts_only": True,
        "coverage_reason": "prior_7d_unverified",
    }
    assert second == {
        "status": "already_sent",
        "period": "morning",
        "date": TARGET_DATE.isoformat(),
    }
    assert len(list(tmp_path.glob("*.sent"))) == 1


def test_facts_only_test_delivery_does_not_change_marker(monkeypatch, tmp_path):
    monkeypatch.setattr(daily_push, "local_today", lambda: TARGET_DATE)
    payload = _daily()
    marker = daily_push._delivery_marker(tmp_path, "synthetic-user", TARGET_DATE, "morning")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("scheduled\n", encoding="utf-8")
    sent = []

    class Service:
        def __init__(self, pushplus_token):
            pass

        def push_daily_profile(self, user_id, profile, period):
            sent.append(profile)
            return {"_pushplus_handler": "ok"}

    monkeypatch.setattr(daily_push, "PushService", Service)
    result = daily_push.deliver_daily_report(
        "synthetic-user",
        "private-token",
        payload,
        period="morning",
        target_date=TARGET_DATE,
        state_dir=tmp_path,
        test_delivery=True,
    )

    assert result["status"] == "test_sent"
    assert result["mode"] == "facts_only"
    assert result["scheduled_delivery_unchanged"] is True
    assert marker.read_text(encoding="utf-8") == "scheduled\n"
    assert len(sent) == 1


def test_verified_history_keeps_normal_morning_plan(monkeypatch, tmp_path):
    monkeypatch.setattr(daily_push, "local_today", lambda: TARGET_DATE)
    payload = _daily(prior_7d_verified=True)
    sent = []

    class Service:
        def __init__(self, pushplus_token):
            pass

        def push_daily_profile(self, user_id, profile, period):
            sent.append(profile)
            return {"_pushplus_handler": "ok"}

    monkeypatch.setattr(daily_push, "PushService", Service)
    result = daily_push.deliver_daily_report(
        "synthetic-user",
        "private-token",
        payload,
        period="morning",
        target_date=TARGET_DATE,
        state_dir=tmp_path,
    )

    assert result["status"] == "sent"
    assert "mode" not in result
    assert "delivery_metadata" not in sent[0]


def test_typed_facts_only_contract_rejects_action_plan():
    daily = _daily()
    engine = MorningBriefingEngine()
    metadata = {"facts_only": True, "coverage_reason": "prior_7d_unverified"}
    report = engine.build(daily, metadata)
    assert report.schema_version == "4.0"
    assert report.action_plan is None
    invalid = engine.build_payload(daily, metadata)
    invalid["action_plan"] = daily["decision"]["action_plan"]
    with pytest.raises(ValueError, match="cannot contain an action plan"):
        MorningBriefing.model_validate(invalid)
    full = engine.build_payload(daily)
    full.pop("action_plan")
    with pytest.raises(ValueError, match="require an action plan"):
        MorningBriefing.model_validate(full)


def test_facts_only_does_not_copy_recovery_or_free_form_interpretations():
    daily = _daily()
    marker = "UNVERIFIED-PRESCRIPTION-IN-RECOVERY"
    daily["features"]["recovery"] = {"state_label": marker, "positive_signal_labels": [marker]}
    daily["features"]["sleep"]["limitations"] = [marker]
    daily["features"]["hrv"]["limitations"] = [marker]
    daily["data_quality"]["status_label"] = marker
    daily["report_context"]["action_plan"] = marker
    report = MorningBriefingEngine().build_payload(daily, {"facts_only": True, "sync_detail": marker})
    assert marker not in repr(report)
    assert all(not section["interpretation"] for section in report["sections"])
    assert report["summary"] == ["训练历史覆盖尚未核验，本次仅发送睡眠和身体状态事实。"]


@pytest.mark.parametrize("blocked", [
    "sleep_unavailable", "wake_missing", "wake_blank", "old_date", "expired_plan",
    "needs_reauth", "token_required", "failed", "cancelled", "quality_insufficient", "quality_missing",
])
def test_facts_only_keeps_non_history_safety_gates(monkeypatch, tmp_path, blocked):
    monkeypatch.setattr(daily_push, "local_today", lambda: TARGET_DATE)
    monkeypatch.setattr(daily_push, "PushService", lambda **kwargs: pytest.fail("blocked report must not send"))
    daily = _daily()
    extra = {}
    if blocked == "sleep_unavailable":
        daily["features"]["sleep"]["status"] = "INSUFFICIENT_DATA"
    elif blocked == "wake_missing":
        daily["features"]["sleep"]["wake_time"] = None
    elif blocked == "wake_blank":
        daily["features"]["sleep"]["wake_time"] = "   "
    elif blocked == "old_date":
        daily["date"] = (TARGET_DATE - timedelta(days=1)).isoformat()
    elif blocked == "expired_plan":
        extra["plan_expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif blocked == "quality_insufficient":
        daily["data_quality"]["status"] = "INSUFFICIENT"
    elif blocked == "quality_missing":
        daily.pop("data_quality")
    else:
        extra["sync_status"] = blocked
        extra["sync_degraded"] = True
    result = daily_push.deliver_daily_report(
        "synthetic-user", "private-token", daily, period="morning",
        target_date=TARGET_DATE, state_dir=tmp_path, **extra,
    )
    assert result["status"] == "deferred"
    assert not list(tmp_path.glob("*.sent"))


def test_missing_history_uses_explicit_facts_only_reason():
    daily = _daily()
    daily["report_context"].pop("training_history")
    daily["features"]["training"].pop("history_coverage", None)
    assert daily_push._morning_facts_only_reason(daily) == "training_history_missing"
