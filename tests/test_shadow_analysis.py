"""Shadow readiness and body-signal rules on synthetic nightly series."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from vitalis.application.shadow_batch import shadow_report
from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint
from vitalis.intelligence.shadow import body_signal_shadow, readiness_shadow

DAY = date(2026, 10, 13)


def _series(metric: str, unit: str, values_by_offset: dict[int, float]) -> list[SeriesPoint]:
    return [
        SeriesPoint(
            metric=metric, value=value, unit=unit, day=DAY - timedelta(days=offset),
            observed_at=DAY - timedelta(days=offset), source="zepp", source_scope="user_fused",
        )
        for offset, value in values_by_offset.items()
    ]


def _raw(**series) -> RawDailyProfile:
    return RawDailyProfile(
        user_id="shadow-synthetic", day=DAY,
        as_of=datetime(2026, 10, 13, 23, tzinfo=timezone.utc), timezone_name="UTC",
        series=series,
    )


def _steady(value: float, days: int = 70, wobble: float = 1.0) -> dict[int, float]:
    return {offset: value + (wobble if offset % 2 else -wobble) for offset in range(days)}


def _readiness(hrv_recent: float, rhr_today: float, sleep_today: float):
    hrv = _steady(50.0, wobble=2.0)
    hrv.update({offset: hrv_recent for offset in range(7)})
    rhr = _steady(52.0)
    rhr[0] = rhr_today
    sleep = _steady(450.0, wobble=15.0)
    sleep[0] = sleep_today
    return readiness_shadow(_raw(
        sleep_hrv=_series("sleep_hrv", "ms", hrv),
        sleep_rhr=_series("sleep_rhr", "bpm", rhr),
        sleep_duration=_series("sleep_duration", "min", sleep),
    ))


def test_readiness_components_move_in_the_expected_direction():
    good = _readiness(hrv_recent=60, rhr_today=48, sleep_today=500)
    poor = _readiness(hrv_recent=40, rhr_today=60, sleep_today=330)
    for name in ("hrv", "sleep", "rhr"):
        assert good["components"][name]["score"] > poor["components"][name]["score"]
    assert good["score"] > poor["score"]
    assert poor["hrv_state"] == "suppressed" and good["hrv_state"] == "elevated"
    assert len(poor["factors"]) == 3 and all(isinstance(item, str) for item in poor["factors"])


def test_readiness_without_hrv_redistributes_weights_and_says_so():
    rhr = _steady(52.0)
    sleep = _steady(450.0, wobble=15.0)
    output = readiness_shadow(_raw(
        sleep_rhr=_series("sleep_rhr", "bpm", rhr),
        sleep_duration=_series("sleep_duration", "min", sleep),
    ))
    payload = output["insight"]["payload"]
    assert "hrv" in payload["missing_components"]
    assert set(payload["applied_weights"]) == {"sleep", "rhr"}
    assert abs(sum(payload["applied_weights"].values()) - 1) < 1e-6
    assert output["insight"]["note"]
    assert output["hrv_state"] == "unavailable"


def test_one_hrv_night_out_of_range_does_not_change_hrv_status():
    hrv = _steady(50.0, wobble=2.0)
    hrv[0] = 20.0
    output = readiness_shadow(_raw(sleep_hrv=_series("sleep_hrv", "ms", hrv)))
    assert output["hrv_state"] == "normal"


def _body(**raised):
    series = {
        "sleep_rhr": _series("sleep_rhr", "bpm", _steady(52.0)),
        "sleep_hrv": _series("sleep_hrv", "ms", _steady(50.0, wobble=2.0)),
        "respiratory_rate": _series("respiratory_rate", "brpm", _steady(14.0, wobble=0.3)),
    }
    for metric, value in raised.items():
        for point_offset in (0, 1):
            series[metric] = [
                point if point.day != DAY - timedelta(days=point_offset)
                else replace(point, value=value)
                for point in series[metric]
            ]
    return body_signal_shadow(_raw(**series))


def test_single_body_signal_does_not_warn():
    assert _body(sleep_rhr=62.0)["warning"] is False


def test_two_body_signals_together_warn_without_naming_a_disease():
    output = _body(sleep_rhr=62.0, sleep_hrv=38.0)
    assert output["warning"] is True
    assert {item["fact"] for item in output["evidence"]} == {"resting_hr", "hrv"}


def test_shadow_report_needs_user_review_for_false_positives():
    rows = [(DAY, {"warning": True, "evidence": [], "inputs": {
        "resting_hr": {"status": "AVAILABLE"}, "hrv": {"status": "AVAILABLE"},
    }})]
    report = shadow_report("body_signal", rows)
    assert report["evaluated_days"] == 1 and report["warning_rate"] == 1.0
    assert "用户确认" in report["note"]


def test_skin_temperature_delta_is_judged_by_z_not_by_percent():
    from vitalis.intelligence.shadow import body_signal_inputs

    temps = _steady(0.02, wobble=0.05)
    temps.update({0: 0.6, 1: 0.6})
    inputs = body_signal_inputs(_raw(skin_temp_delta=_series("skin_temp_delta", "C", temps)))
    item = inputs["skin_temperature_delta"]
    assert item["status"] == "AVAILABLE" and item["direction"] == "above"
    assert item["percent"] is None and item["robust_z"] > 1.5
