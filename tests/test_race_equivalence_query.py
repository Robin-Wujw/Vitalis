from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from vitalis.application.health_query import HealthQuery
from vitalis.application.ports import HealthWorkoutRead


def test_saved_run_equivalence_reads_exact_selected_user_record_without_samples():
    calls = []
    def detail(user, workout_id, source, *, include_samples):
        calls.append((user, workout_id, source, include_samples))
        if (user, workout_id, source) != ("runner", "race-1", "mock"):
            return None
        return HealthWorkoutRead(source="mock", workout_id="race-1", detail_synced=True, detail=None,
            data={"type": "running", "distance_km": 5.0, "duration": 25,
                  "started_at": "2026-01-02T08:00:00Z", "ended_at": "2026-01-02T08:25:00Z"}), []
    query = HealthQuery(SimpleNamespace(workout_detail=detail),
                        now_factory=lambda: datetime(2026, 1, 3, tzinfo=timezone.utc))
    result = query.race_equivalence("runner", "race-1", "mock")
    assert result["equivalence"]["seconds"] == pytest.approx(6900.299, abs=.01)
    assert result["effort_basis"] == "scenario"
    assert result["user_id"] == "runner"
    assert query.race_equivalence("other", "race-1", "mock") is None
    assert calls == [("runner", "race-1", "mock", False), ("other", "race-1", "mock", False)]


def test_running_equivalence_rejects_unfinished_duration_fallback():
    row = HealthWorkoutRead(source="mock", workout_id="ongoing", detail_synced=False, detail=None,
        data={"type": "running", "distance_km": 5, "duration": 25,
              "started_at": "2026-01-03T07:55:00Z"})
    query = HealthQuery(SimpleNamespace(workout_detail=lambda *a, **kw: (row, [])),
                        now_factory=lambda: datetime(2026, 1, 3, 8, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="future_workout"):
        query.race_equivalence("runner", "ongoing", "mock")


def test_running_equivalence_rejects_future_record():
    row = HealthWorkoutRead(source="mock", workout_id="future", detail_synced=False, detail=None,
        data={"type": "running", "distance_km": 5, "duration": 25,
              "started_at": "2099-01-02T08:00:00Z", "ended_at": "2099-01-02T08:25:00Z"})
    query = HealthQuery(SimpleNamespace(workout_detail=lambda *a, **kw: (row, [])),
                        now_factory=lambda: datetime(2026, 1, 3, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="future_workout"):
        query.race_equivalence("runner", "future", "mock")
