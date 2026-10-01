from vitalis.intelligence.monthly_briefing import MonthlyBriefingEngine


def test_monthly_briefing_uses_calendar_month_denominators():
    payload = {
        "analysis_run_id": "calendar-month-report",
        "user_id": "synthetic-user",
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "generated_at": "2026-02-01T08:00:00+00:00",
        "report_context": {
            "period_mode": "calendar",
            "as_of": "2026-02-01T08:00:00+00:00",
            "timezone": "Asia/Shanghai",
        },
        "data_quality": {"sleep_days": 31, "limitations": []},
        "facts": {
            "sleep": {
                "available_days": 31,
                "previous_available_days": 31,
                "average_minutes": 430,
                "previous_average_minutes": 420,
                "change_percent": 2.4,
            },
            "recovery": {"streams": []},
            "training": {
                "coverage_status": "COMPLETE",
                "record_days": 31,
                "unknown_days": 0,
                "totals_are_partial": False,
                "workout_count": 4,
            },
            "activity": {
                "metrics": [{
                    "metric": "steps",
                    "unit": "steps",
                    "available_days": 31,
                    "complete_days": 31,
                    "previous_available_days": 31,
                    "previous_complete_days": 31,
                    "total": 200000,
                    "average": 6451,
                    "previous_average": 6200,
                    "change_percent": 4.0,
                    "totals_are_partial": False,
                    "provenance": {"source": "synthetic", "source_scope": "daily"},
                }],
                "available_days": 31,
            },
            "feedback": {"response_count": 0},
        },
        "inferences": {"key_changes": [], "personal_associations": []},
        "actions": {"recommendations": []},
    }

    report = MonthlyBriefingEngine().build(payload)
    text = report.model_dump_json()

    assert "31/31 天" in text
    assert "/28" not in text
