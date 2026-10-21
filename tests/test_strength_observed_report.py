from datetime import date
from types import SimpleNamespace

import pytest

from vitalis.adapters.zepp.parser import ZEPP_STRENGTH_LAP_LABELS
from vitalis.intelligence.contracts import StrengthExerciseInput, TrainingPreferences
from vitalis.intelligence.decision import DecisionEngine
from vitalis.intelligence.evening_briefing import EveningBriefingEngine
from vitalis.intelligence.profile import RawDailyProfile
from vitalis.intelligence.report_rendering import render_report
from vitalis.intelligence.strength import (
    StrengthAnalyzer,
    estimate_one_rep_max,
    normalize_exercise,
    strength_session_capacity,
)

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


def test_strength_capacity_separates_physical_load_from_muscle_and_effort_qualification():
    assert estimate_one_rep_max(60, 8, unit="kg", basis="total", rir=0) == 76.0
    summary = strength_session_capacity(
        {
            "explicit_exercises": [{
                "source": "user_confirmed", "exercise_id": "bench_press", "exercise_name": "卧推",
                "sets": 3, "repetitions": 8, "weight_kg": 60,
                "weight_unit": "kg", "weight_basis": "total",
            }],
            "observed_sets": [{
                "source": "lap_62", "vendor_exercise_code": 999999,
                "repetitions": 12, "weight_value": 20,
            }],
        },
    )

    assert summary["capacity"][0]["volume"] == 1440.0
    assert summary["capacity"][0]["estimated_1rm"] is None
    assert summary["capacity"][0]["personal_record"] is False
    assert next(x for x in summary['muscle_doses'] if x['muscle_group'] == 'chest')['direct_sets'] == 3
    assert not any(item.get("exercise_id") == "999999" for item in summary["capacity"])


@pytest.mark.parametrize("field", ["weight_unit", "weight_basis"])
def test_strength_capacity_abstains_when_weight_qualification_is_missing(field):
    exercise = {
        "source": "user_confirmed", "exercise_id": "bench_press", "exercise_name": "卧推",
        "sets": 3, "repetitions": 8, "weight_kg": None, "weight_value": 60,
        "weight_unit": "kg", "weight_basis": "total",
    }
    exercise[field] = None

    summary = strength_session_capacity({"explicit_exercises": [exercise]})

    assert summary["capacity"] == []
    assert summary["muscle_doses"]
    assert summary["limitations"]


@pytest.mark.parametrize("weight", [None, 0, 10])
def test_bodyweight_does_not_supply_effective_mass_for_capacity_or_one_rep_max(weight):
    confirmed = normalize_exercise(
        "synthetic-bodyweight", "observed-strength", 1,
        StrengthExerciseInput(
            exercise_id="pull_up", exercise_name="引体向上", sets=3, repetitions=8,
            weight_kg=weight, weight_unit="kg", weight_basis="bodyweight",
        ),
    )
    raw = RawDailyProfile(user_id="synthetic-bodyweight", day=TARGET)
    raw.workouts = [_workout({}, confirmed=[confirmed])]

    session = StrengthAnalyzer().analyze(raw).recent_sessions[0]
    volume_points = [
        item for item in StrengthAnalyzer().trend_points(raw)
        if item["metric"] == "strength_volume"
    ]

    assert estimate_one_rep_max(weight, 8, unit="kg", basis="bodyweight") is None
    assert session.capacity_volume_kg is None
    assert session.explicit_exercises[0].canonical_qualified is True
    assert session.explicit_exercises[0].estimated_1rm_kg is None
    assert volume_points and all(item["value"] is None for item in volume_points)


def test_vendor_pounds_keep_observations_and_normalize_explicit_capacity_to_kilograms():
    raw = RawDailyProfile(user_id="synthetic-pounds", day=TARGET)
    raw.workouts = [_workout({"strength_sets": [{
        "source": "strength_sets", "order": 1, "exercise_id": "bench_press",
        "exercise_name": "卧推", "repetitions": 8, "weight_value": 20,
        "weight_unit": "lb", "weight_basis": "total",
    }, {
        "source": "lap_62", "order": 2, "exercise_name": "卧推",
        "repetitions": 8, "weight_value": 20, "weight_unit": "lb",
        "weight_basis": "total",
    }]})]

    session = StrengthAnalyzer().analyze(raw).recent_sessions[0]

    assert [(item.weight_value, item.weight_unit) for item in session.observed_sets] == [
        (20, "lb"), (20, "lb"),
    ]
    explicit, = session.explicit_exercises
    assert explicit.weight_kg == pytest.approx(9.0718474)
    assert explicit.weight_unit == "kg"
    assert explicit.source == "vendor_explicit"
    assert session.capacity_volume_kg == 145.1
    assert session.dose_coverage.volume_sets == 2
    assert explicit.estimated_1rm_kg is None


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize(("weight_fields", "expected"), [
    ({}, "自重"),
    ({"weight_value": 0, "weight_unit": "kg"}, "自重"),
    ({"weight_kg": 0}, "自重"),
    ({"weight_value": 10, "weight_unit": "kg"}, "自重 + 负重 10 kg"),
    ({"weight_kg": 10}, "自重 + 负重 10 kg"),
    ({"weight_value": 12.5, "weight_unit": "lb"}, "自重 + 负重 12.5 lb"),
])
def test_bodyweight_and_added_load_match_evening_facts_and_rendered_reports(explicit, weight_fields, expected):
    item = {
        "source": "user_confirmed" if explicit else "lap_62",
        "order": 1, "exercise_name": "引体向上", "repetitions": 8,
        "weight_basis": "bodyweight", **weight_fields,
    }
    session = {
        "date": TARGET.isoformat(), "duration_minutes": 30,
        "observed_sets": [] if explicit else [item],
        "explicit_exercises": [item] if explicit else [],
    }

    report = EveningBriefingEngine().build(_payload(session))
    facts = "\n".join(report.sections[0].facts)

    assert expected in facts
    for target in ("markdown", "html"):
        content = render_report(report, target).content
        assert expected in content
        if expected == "自重":
            assert "自重 0" not in content
            assert "负重 0" not in content
    if expected == "自重":
        assert "0 千克" not in facts
        assert "负重 0" not in facts


def test_zero_weight_without_bodyweight_evidence_stays_a_numeric_observation():
    session = {
        "date": TARGET.isoformat(), "duration_minutes": 30,
        "observed_sets": [{
            "source": "lap_62", "order": 1, "exercise_name": "未确认动作",
            "repetitions": 8, "weight_value": 0,
        }],
        "explicit_exercises": [],
    }
    report = EveningBriefingEngine().build(_payload(session))

    for content in (
        "\n".join(report.sections[0].facts),
        render_report(report, "markdown").content,
        render_report(report, "html").content,
    ):
        assert "负重 0" in content
        assert "自重" not in content


@pytest.mark.parametrize("unit", ["lb", None])
def test_evening_keeps_observed_weight_value_and_unit_ahead_of_converted_kilograms(unit):
    session = {
        "date": TARGET.isoformat(), "duration_minutes": 30,
        "observed_sets": [{
            "source": "lap_62", "order": 1, "exercise_name": "卧推",
            "repetitions": 8, "weight_value": 20, "weight_unit": unit,
            "weight_kg": 9.0718474,
        }],
        "explicit_exercises": [],
    }
    report = EveningBriefingEngine().build(_payload(session))

    for content in (
        "\n".join(report.sections[0].facts),
        render_report(report, "markdown").content,
        render_report(report, "html").content,
    ):
        assert ("20 lb" if unit else "负重 20（单位未") in content
        assert "9.1" not in content
        assert "20 kg" not in content
        assert "20 千克" not in content


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
        "order": len(ZEPP_STRENGTH_LAP_LABELS) + 1,
        "vendor_exercise_code": 999999,
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
    assert "动作代码 999999（名称未确认）" in text
    assert any("已核验编码对照" in item for item in report.sections[0].limitations)


def test_confirmed_exercises_win_over_observed_rows_but_observations_remain_in_session():
    confirmed = normalize_exercise(
        "synthetic-confirmed",
        "confirmed-workout",
        1,
        StrengthExerciseInput(exercise_name="卧推", sets=4, repetitions=8, weight_kg=60),
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


def test_evening_digest_groups_consecutive_sets_without_merging_a_b_a():
    raw = RawDailyProfile(user_id="synthetic-ordered", day=TARGET)
    raw.workouts = [_workout({"strength_sets": [
        {"source": "lap_62", "order": 1, "exercise_name": "卧推", "repetitions": 8},
        {"source": "lap_62", "order": 2, "exercise_name": "卧推", "repetitions": 9},
        {"source": "lap_62", "order": 3, "exercise_name": "划船", "repetitions": 10},
        {"source": "lap_62", "order": 4, "exercise_name": "卧推", "repetitions": 8},
        {"source": "lap_62", "order": 5, "vendor_exercise_code": 999999, "repetitions": 8},
    ]})]
    session = StrengthAnalyzer()._session(raw, raw.workouts[0], None)
    payload = _payload(session.model_dump(mode="json"))
    report = EveningBriefingEngine().build(payload)
    rendered = render_report(report, target="markdown")
    title, digest = rendered.title, rendered.content
    detail = "\n".join(report.sections[0].facts)

    assert title.startswith("Vitalis ")
    # Canonical Markdown keeps adjacent grouping while preserving A/B/A order
    # and the observed dose; it is not a lossy averaged digest.
    assert digest.index("卧推") < digest.index("划船") < digest.index("卧推", digest.index("划船"))
    assert "负重未记录 · 2 组 · 8 / 9 次" in digest
    assert "负重未记录 · 1 组 · 10 次" in digest
    assert "负重未记录 · 1 组 · 8 次" in digest
    assert "3 × 10" not in digest
    assert "第 1 组：卧推；8 次；负重未记录。" in detail
    assert "第 5 组：动作代码 999999（名称未确认）" in detail
    assert session.explicit_exercises == []


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
    rendered = render_report(
        EveningBriefingEngine().build(_payload(session)), target="html"
    )
    title, html = rendered.title, rendered.content

    assert title.startswith("Vitalis ")
    assert '&lt;img src=&quot;x&quot; onerror=&quot;alert(1)&quot;&gt;' in html
    assert '<img src="x"' not in html
