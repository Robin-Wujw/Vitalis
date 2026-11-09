from copy import deepcopy
from datetime import date, datetime, timezone

import pytest

from vitalis.intelligence.analyzers import TrainingAnalyzer
from vitalis.intelligence.profile import RawDailyProfile
from vitalis.intelligence.report_presentation import daily_presentation
from vitalis.intelligence.report_rendering import render_report, visible_text
from vitalis.intelligence.evening_briefing import EveningBriefingEngine
from vitalis.intelligence.public_reports import to_public_report_view
from tests.test_report_content import synthetic_daily_fixture


DAY = date(2026, 9, 5)


def _workout(identity, kind, minutes, distance=None, sets=None, *, source="zepp", day=DAY):
    return {
        "workout_id": identity, "source": source, "local_day": day,
        "started_at": datetime(day.year, day.month, day.day, 8, tzinfo=timezone.utc),
        "data": {"type": kind, "training_family": "strength" if kind == "strength" else "aerobic",
                 "sport_mode": kind, "sport_mode_label": "力量训练" if kind == "strength" else "跑步",
                 "duration": minutes, "distance_km": distance, "vendor_reported_sets": sets},
    }


def _analyze(workouts):
    raw = RawDailyProfile(user_id="synthetic", day=DAY, timezone_name="Asia/Shanghai",
                          as_of=datetime(2026, 9, 5, 15, tzinfo=timezone.utc), workouts=workouts)
    return TrainingAnalyzer().analyze(raw, {}).model_dump(mode="json")


def test_multiple_runs_and_strength_keep_daily_totals_separate_and_do_not_mutate_inputs():
    workouts = [_workout("r1", "running", 10, 2), _workout("r2", "running", 30, 5),
                _workout("s1", "strength", 20, sets=3), _workout("s2", "strength", 15, sets=4)]
    before = deepcopy(workouts)
    summary = _analyze(workouts)["day_summaries"][0]
    assert workouts == before
    assert summary["date"] == DAY.isoformat()
    assert summary["date_basis"] == "activity_day"
    assert summary["timezone"] == "Asia/Shanghai"
    assert summary["session_count"] == 4
    assert summary["duration"]["value"] == 75
    groups = {group["kind"]: group for group in summary["groups"]}
    assert groups["running"]["session_count"] == 2
    assert groups["running"]["distance"]["value"] == 7
    assert groups["strength"]["sets"]["value"] == 7
    assert groups["strength"]["duration"]["value"] == 35
    assert {item["workout_id"] for group in summary["groups"] for item in group["records"]} == {"r1", "r2", "s1", "s2"}


def test_missing_values_are_partial_and_calendar_dates_and_sources_are_distinct():
    workouts = [_workout("same", "running", 10, 2),
                _workout("same", "running", None, source="other"),
                _workout("yesterday", "running", 20, 3, day=date(2026, 9, 4)),
                _workout("bike", "cycling", 30, 8)]
    summary = next(item for item in _analyze(workouts)["day_summaries"] if item["date"] == DAY.isoformat())
    run = next(group for group in summary["groups"] if group["kind"] == "running")
    assert summary["session_count"] == 3
    assert summary["duration"]["value"] is None
    assert run["distance"]["value"] is None
    assert run["distance"]["sample_count"] == 1
    assert run["distance"]["status"] == "PARTIAL"
    assert {row["source"] for row in run["records"]} == {"zepp", "other"}


@pytest.mark.parametrize("duration,observed,expected", [(0, [], None), (0, ["duration"], 0), (None, [], None)])
def test_duration_zero_requires_observation_and_duplicate_identity_counts_once(duration, observed, expected):
    workout = _workout("r1", "running", duration)
    workout["data"]["observed_fields"] = observed
    summary = _analyze([workout, deepcopy(workout)])["day_summaries"][0]
    assert summary["session_count"] == 1
    assert summary["duration"]["value"] == expected


def test_training_details_keep_original_strength_sets_and_chronological_segments():
    from tests.test_report_presentation import _daily

    payload = _daily()
    training = payload["features"]["training"]
    training["recent_workouts"] = [
        {"date": "2026-10-07", "source": "zepp", "workout_id": "late", "started_at": "2026-10-07T12:00:00Z"},
        {"date": "2026-10-07", "source": "zepp", "workout_id": "early", "started_at": "2026-10-07T06:00:00Z"},
    ]
    training["strength"]["recent_sessions"] = [
        {"date": "2026-10-07", "source": "zepp", "workout_id": identity,
         "explicit_exercises": [{"exercise_id": "curl", "exercise_name": "弯举", "sets": 1, "repetitions": reps}
                                for reps in repetitions]}
        for identity, repetitions in (("early", [12, 10, 8]), ("late", [8, 6]))
    ]
    result = daily_presentation(payload)["training"]
    assert [row["workout_id"] for row in result] == ["late", "early"]
    assert [[row["repetitions"] for row in item["exercises"][0]["sets"]] for item in result] == [[8, 6], [12, 10, 8]]
    content = render_report(payload, "markdown").content
    assert content.index("06:00") < content.index("12:00")
    assert "12 / 10 / 8 次" in content


def test_evening_and_daily_render_one_overview_and_retain_segment_details():
    payload = synthetic_daily_fixture()
    payload["features"]["training"] = _analyze([
        _workout("r1", "running", 10, 2), _workout("r2", "running", 30, 5),
        _workout("s1", "strength", 20, sets=3), _workout("s2", "strength", 15, sets=4),
    ])
    presentation = daily_presentation(payload)
    assert len(presentation["training"]) == 4
    assert presentation["training_summary"]["session_count"] == 4
    for report in (payload, EveningBriefingEngine().build(payload),
                   EveningBriefingEngine().build(payload, {"facts_only": True})):
        view = to_public_report_view(report)
        block = next(block for block in view.blocks if block.section_id == "training")
        assert block.training_summary["session_count"] == 4
        assert len(block.workouts) == 4
        for target in ("markdown", "html"):
            content = visible_text(render_report(report, target).content, target)
            assert content.count("今日训练总览") == 1
            assert "跑步 2 段" in content
            assert "力量 2 段" in content
            assert "7 公里" in content
            assert "设备记录 7 组" in content
            assert "第 1 段" in content
            assert "第 4 段" in content
