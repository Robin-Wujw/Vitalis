from datetime import date
from types import SimpleNamespace

from vitalis.connectors.zepp.parser import ZEPP_STRENGTH_LAP_LABELS
from vitalis.intelligence.contracts import StrengthExerciseInput, TrainingPreferences
from vitalis.intelligence.evening_briefing import EveningBriefingEngine
from vitalis.intelligence.strength import StrengthAnalyzer, normalize_exercise
from vitalis.services.push_service import _render_evening, _render_report_html
from vitalis.intelligence.decision import DecisionEngine
from vitalis.intelligence.profile import RawDailyProfile


TARGET = date(2026, 9, 5)


def _workout(detail, confirmed=None):
    return {
        "workout_id": "observed-strength",
        "source": "zepp",
        "local_day": TARGET,
        "detail_available": True,
        "detail": detail,
        "confirmed_exercises": confirmed or [],
        "samples": [],
        "data": {"type": "strength", "training_family": "strength", "duration": 45},
    }


def _payload(session):
    return {
        "date": TARGET.isoformat(),
        "features": {
            "training": {
                "recent_workouts": [],
                "running": {"recent_sessions": []},
                "strength": {"recent_sessions": [session]},
            },
            "activity": {},
        },
    }


def test_observed_sets_keep_a_b_a_order_and_lap_does_not_become_explicit():
    raw = RawDailyProfile(user_id="synthetic-observed", day=TARGET)
    raw.workouts = [_workout({"strength_sets": [
        {"source": "strength_sets", "order": 1, "exercise_name": "卧推", "repetitions": 8, "weight_kg": 60},
        {"source": "strength_sets", "order": 3, "exercise_name": "划船", "repetitions": 10, "weight_kg": 40},
        {"source": "strength_sets", "order": 5, "exercise_name": "卧推", "repetitions": 8, "weight_kg": 60},
        {"source": "lap_62", "order": 7, "vendor_exercise_code": 62, "repetitions": 12},
    ]})]

    session = StrengthAnalyzer()._session(raw, raw.workouts[0], None)

    assert [(item.order, item.exercise_name, item.source) for item in session.observed_sets] == [
        (1, "卧推", "strength_sets"),
        (3, "划船", "strength_sets"),
        (5, "卧推", "strength_sets"),
        (7, None, "lap_62"),
    ]
    assert all(item.source != "lap_62" for item in session.explicit_exercises)
    assert session.total_sets == 3


def test_evening_prefers_observed_rows_and_preserves_units_and_missing_weight():
    raw = RawDailyProfile(user_id="synthetic-observed", day=TARGET)
    workout = _workout({"strength_sets": [
        {"source": "strength_sets", "order": 1, "exercise_name": "卧推", "repetitions": 8, "weight_kg": 60},
        {"source": "strength_sets", "order": 3, "vendor_exercise_code": 62, "repetitions": 10, "weight_value": 12.5, "weight_unit": "lb"},
        {"source": "strength_sets", "order": 5, "exercise_name": "卧推", "repetitions": 8, "weight_value": -1},
    ]})
    session = StrengthAnalyzer()._session(raw, workout, None)
    report = EveningBriefingEngine().build(_payload(session.model_dump(mode="json")))
    facts = report.sections[0].facts
    rows = [item for item in facts if item.startswith("第 ")]

    assert [row.split("：", 1)[0] for row in rows] == ["第 1 组", "第 3 组", "第 5 组"]
    assert "第 1 组：卧推；8 次；60 千克。" in rows
    assert "动作代码 62（名称未确认）" in rows[1]
    assert "12.5 lb" in rows[1]
    assert "第 5 组：卧推；8 次；负重未记录。" in rows
    assert "自重" not in "\n".join(rows)
    assert not any(item.startswith("动作 卧推：") for item in facts)


def test_mapped_lap_names_render_while_unknown_codes_stay_unconfirmed():
    observed_sets = [
        {
            "source": "lap_62",
            "order": order,
            "vendor_exercise_code": code,
            "exercise_name": name,
            "limitations": ["exercise_name_reference_mapping"],
            "repetitions": 8,
        }
        for order, (code, name) in enumerate(ZEPP_STRENGTH_LAP_LABELS.items(), start=1)
    ]
    observed_sets.append({
        "source": "lap_62",
        "order": 6,
        "vendor_exercise_code": 801,
        "limitations": ["exercise_name_unverified"],
        "repetitions": 8,
    })
    raw = RawDailyProfile(user_id="synthetic-mapped-lap", day=TARGET)
    raw.workouts = [_workout({"strength_sets": observed_sets})]

    session = StrengthAnalyzer()._session(raw, raw.workouts[0], None)
    mapped_only_raw = RawDailyProfile(user_id="synthetic-mapped-only", day=TARGET)
    mapped_only_raw.workouts = [_workout({"strength_sets": observed_sets[:-1]})]
    mapped_only_session = StrengthAnalyzer()._session(mapped_only_raw, mapped_only_raw.workouts[0], None)
    report = EveningBriefingEngine().build(_payload(session.model_dump(mode="json")))
    text = "\n".join(report.sections[0].facts)

    assert not any("观测组动作名称未确认" in item for item in mapped_only_session.limitations)
    assert session.explicit_exercises == []
    assert session.focus == "UNKNOWN"
    assert session.muscle_groups == []
    assert any("已核验编码对照" in item for item in session.limitations)
    assert any("观测组动作名称未确认" in item for item in session.limitations)
    for name in ZEPP_STRENGTH_LAP_LABELS.values():
        assert name in text
    assert "动作代码 801（名称未确认）" in text
    assert any("已核验编码对照" in item for item in report.sections[0].limitations)


def test_confirmed_exercises_win_over_observed_rows_but_observations_remain_in_session():
    confirmed = normalize_exercise(
        "synthetic-confirmed",
        "confirmed-workout",
        1,
        StrengthExerciseInput(exercise_name="卧推", sets=4, repetitions="8 次", weight_kg=60),
        "PUSH",
    )
    raw = RawDailyProfile(user_id="synthetic-confirmed", day=TARGET)
    workout = _workout(
        {"strength_sets": [{"source": "strength_sets", "order": 1, "exercise_name": "深蹲", "repetitions": 10}]},
        confirmed=[confirmed],
    )
    session = StrengthAnalyzer()._session(raw, workout, None)
    report = EveningBriefingEngine().build(_payload(session.model_dump(mode="json")))
    text = "\n".join(report.sections[0].facts)

    assert session.observed_sets[0].exercise_name == "深蹲"
    assert "动作 卧推：4 组；每组 8 次；60 千克" in text
    assert "第 1 组" not in text
    assert "深蹲" not in text


def test_lap_observation_does_not_raise_coverage_focus_or_prescription():
    raw = RawDailyProfile(user_id="synthetic-lap", day=TARGET)
    raw.workouts = [_workout({"strength_sets": [
        {
            "source": "lap_62",
            "order": 4,
            "vendor_exercise_code": 64,
            "exercise_name": ZEPP_STRENGTH_LAP_LABELS[64],
            "limitations": ["exercise_name_reference_mapping"],
            "repetitions": 12,
        },
    ]})]

    analysis = StrengthAnalyzer().analyze(raw)
    session = analysis.recent_sessions[0]
    planned = DecisionEngine()._strength(
        TrainingPreferences(user_id="synthetic-lap"),
        SimpleNamespace(strength=analysis),
        "PRIMARY",
        "moderate",
    )

    assert analysis.explicit_session_coverage == 0
    assert session.explicit_exercises == []
    assert session.focus == "UNKNOWN"
    assert session.muscle_groups == []
    assert planned.code == "strength_full_body"
    assert ZEPP_STRENGTH_LAP_LABELS[64] not in " ".join(step.name for step in planned.steps)


def test_observed_report_uses_existing_html_escape_path():
    session = {
        "date": TARGET.isoformat(),
        "focus": "UNKNOWN",
        "duration_minutes": 30,
        "observed_sets": [{
            "source": "lap_62",
            "order": 1,
            "vendor_exercise_code": 62,
            "exercise_name": '<img src="x" onerror="alert(1)">',
            "repetitions": 8,
            "weight_value": 10,
            "weight_unit": "lb",
        }],
        "explicit_exercises": [],
    }
    title, lines = _render_evening(_payload(session))
    html = _render_report_html(lines)

    assert title.startswith("Vitalis 晚报")
    assert '&lt;img src="x" onerror="alert(1)"&gt;' in html
    assert '<img src="x"' not in html
