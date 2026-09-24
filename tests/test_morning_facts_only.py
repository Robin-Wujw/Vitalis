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
    assert briefing["summary"] == ["部分运动记录来源还未查全；已记录的训练照常展示，今天暂不生成训练安排。"]
    assert briefing["cautions"] == []


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
    assert message.title == f"Vitalis 晨报 · {TARGET_DATE.isoformat()}"
    assert "昨晚睡眠" in message.body
    assert "今早恢复信号" in message.body
    assert "今天的安排" not in message.body
    assert "部分运动记录来源还未查全" in message.body
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
    assert report["summary"] == ["部分运动记录来源还未查全；已记录的训练照常展示，今天暂不生成训练安排。"]


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


def test_facts_only_renders_dated_activity_and_observed_training_without_a_plan():
    daily = _daily()
    day = TARGET_DATE.isoformat()
    yesterday = (TARGET_DATE - timedelta(days=1)).isoformat()
    daily["report_context"]["timezone"] = "Asia/Shanghai"
    daily["report_context"]["previous_day_activity"] = {
        "user_id": daily["user_id"], "date": yesterday,
        "as_of": "2026-08-29T04:00:00+00:00",
        "activity": {
            "status": "AVAILABLE",
            "steps": {"value": 8200, "unit": "steps", "observed_at": yesterday,
                      "provenance": {"source": "zepp", "source_scope": "user_fused"}},
            "energy": [{"value": 420, "unit": "kcal", "role": "unspecified",
                        "observed_at": yesterday,
                        "provenance": {"source": "zepp", "source_scope": "device"}}],
        },
    }
    for key in ("steps", "distance_km", "active_minutes"):
        daily["features"]["activity"][key]["observed_at"] = day
    for item in daily["features"]["activity"]["energy"]:
        item["observed_at"] = day
    daily["features"]["training"]["recent_workouts"].append({
        "date": yesterday, "type_label": "力量训练", "sport_mode_label": "力量训练",
        "training_family": "strength", "duration_minutes": 52, "distance_km": 0,
        "calories_kcal": 200, "heart_rate_avg_bpm": 112, "vendor_reported_sets": 8,
    })
    marker = "UNSAFE-PRESCRIPTION"
    daily["features"]["recovery"].update({
        "state_label": marker, "positive_signal_labels": [marker],
    })
    report = MorningBriefingEngine().build_payload(daily, {"facts_only": True})
    MorningBriefing.model_validate(report)
    keys = [section["key"] for section in report["sections"]]
    assert keys == ["sleep", "recovery", "yesterday_activity", "observed_training", "today_activity"]
    text = str(report)
    assert "步数 8,200 步" in text
    assert "设备估算热量（统计范围待确认） 420 千卡" in text
    assert "已记录力量训练" in text and "设备记录组数 8 组" in text
    assert "平均心率 112 次/分钟" in text
    assert "距离 0 公里" not in text
    assert marker not in text
    assert "action_plan" not in text


def test_facts_only_summarizes_yesterday_observed_sets_without_prescribing():
    daily = _daily()
    yesterday = (TARGET_DATE - timedelta(days=1)).isoformat()
    daily["features"]["training"]["recent_workouts"].append({
        "date": yesterday, "type_label": "力量训练", "duration_minutes": 52,
        "training_family": "strength", "vendor_reported_sets": 4,
    })
    daily["features"]["training"]["strength"]["recent_sessions"] = [{
        "date": yesterday, "explicit_exercises": [],
        "observed_sets": [
            {"order": 1, "source": "lap_62", "exercise_name": "引体向上", "vendor_exercise_code": 64},
            {"order": 2, "source": "lap_62", "exercise_name": "引体向上", "vendor_exercise_code": 64},
            {"order": 3, "source": "lap_62", "vendor_exercise_code": 801},
        ],
    }]

    report = MorningBriefingEngine().build_payload(daily, {"facts_only": True})
    observed = next(section for section in report["sections"] if section["key"] == "observed_training")
    assert "逐组观测动作：引体向上 2 组、动作代码 801（名称未确认） 1 组。" in observed["facts"]
    assert not observed["interpretation"]
    assert "action_plan" not in report


def test_facts_only_distinguishes_vendor_explicit_from_user_confirmed_actions():
    daily = _daily()
    yesterday = (TARGET_DATE - timedelta(days=1)).isoformat()
    daily["features"]["training"]["strength"]["recent_sessions"] = [{
        "date": yesterday,
        "explicit_exercises": [{
            "source": "vendor_explicit", "exercise_name": "侧平举", "sets": 4,
        }],
        "observed_sets": [],
    }]

    report = MorningBriefingEngine().build_payload(daily, {"facts_only": True})
    facts = next(section for section in report["sections"] if section["key"] == "observed_training")["facts"]
    assert facts == ["设备明确动作：侧平举 4 组。"]
    assert "action_plan" not in report


def test_facts_only_keeps_vendor_scores_separate_from_observed_hrv_trend():
    daily = _daily()
    daily["features"]["hrv"].update({
        "recent_7d_median_ms": 66, "recent_7d_days": 5,
        "previous_7d_median_ms": 61, "previous_7d_days": 4,
    })
    daily["features"]["recovery"].update({
        "vendor_readiness": 82, "vendor_charge": 75,
        "vendor_readiness_components": {"身体": 80, "心理": 73},
        "state_label": "UNSAFE-RECOVERY-CONCLUSION",
    })
    report = MorningBriefingEngine().build_payload(daily, {"facts_only": True})
    facts = next(section for section in report["sections"] if section["key"] == "recovery")["facts"]

    assert any("近 7 日同源 睡眠 HRV：中位数 66 毫秒（有效 5 天）" in item for item in facts)
    assert any("此前 7 日同源 睡眠 HRV：中位数 61 毫秒（有效 4 天）" in item for item in facts)
    assert "设备准备度评分 82/100（厂商参考值）" in facts
    assert "设备准备度（身体）80/100（厂商参考值）" in facts
    assert "UNSAFE-RECOVERY-CONCLUSION" not in repr(report)


@pytest.mark.parametrize("bad_field", ["user_id", "date", "as_of"])
def test_facts_only_rejects_untrusted_previous_day_context(bad_field):
    daily = _daily()
    yesterday = (TARGET_DATE - timedelta(days=1)).isoformat()
    daily["report_context"]["previous_day_activity"] = {
        "user_id": daily["user_id"], "date": yesterday,
        "as_of": "2026-08-29T04:00:00+00:00",
        "activity": {"status": "AVAILABLE", "steps": {
            "value": 100, "unit": "steps", "observed_at": yesterday,
            "provenance": {"source": "zepp", "source_scope": "device"},
        }},
    }
    daily["report_context"]["previous_day_activity"][bad_field] = {
        "user_id": "another-user", "date": "2026-08-20",
        "as_of": "2026-08-28T12:00:00+00:00",
    }[bad_field]
    report = MorningBriefingEngine().build_payload(daily, {"facts_only": True})
    assert "yesterday_activity" not in [section["key"] for section in report["sections"]]
