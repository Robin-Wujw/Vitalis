"""Shadow readiness and body-signal rules on synthetic nightly series."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from vitalis.application.shadow_batch import shadow_report
from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint
from vitalis.intelligence.readiness import body_signal_warning, readiness_score

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
    return readiness_score(_raw(
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
    output = readiness_score(_raw(
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
    output = readiness_score(_raw(sleep_hrv=_series("sleep_hrv", "ms", hrv)))
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
    return body_signal_warning(_raw(**series))


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
    from vitalis.intelligence.readiness import body_signal_inputs

    temps = _steady(0.02, wobble=0.05)
    temps.update({0: 0.6, 1: 0.6})
    inputs = body_signal_inputs(_raw(skin_temp_delta=_series("skin_temp_delta", "C", temps)))
    item = inputs["skin_temperature_delta"]
    assert item["status"] == "AVAILABLE" and item["direction"] == "above"
    assert item["percent"] is None and item["robust_z"] > 1.5


def test_readiness_band_sets_the_recovery_state_and_keeps_the_vote():
    from vitalis.intelligence.contracts import Availability, RecoveryFeatures, RecoveryState
    from vitalis.intelligence.readiness import apply_readiness, readiness_band

    vote = RecoveryFeatures(status=Availability.AVAILABLE, state=RecoveryState.SUPPRESSED)
    normal = apply_readiness(vote, {"score": 70, "factors": ["静息心率接近你平时"]})
    assert normal.state == RecoveryState.NORMAL and normal.vote_state == RecoveryState.SUPPRESSED
    assert normal.readiness_score == 70 and normal.readiness_factors == ["静息心率接近你平时"]
    assert apply_readiness(vote.model_copy(update={"state": RecoveryState.NORMAL}), {"score": 54}).state == RecoveryState.SUPPRESSED
    assert apply_readiness(vote, {"score": 88}).state == RecoveryState.GOOD
    unknown = apply_readiness(vote, {"score": None})
    assert unknown.state == RecoveryState.SUPPRESSED and unknown.readiness_score is None
    insufficient = RecoveryFeatures(status=Availability.INSUFFICIENT_DATA, state=RecoveryState.INSUFFICIENT_DATA)
    scored = apply_readiness(insufficient, {"score": 66})
    assert scored.state == RecoveryState.NORMAL and scored.status == Availability.AVAILABLE
    assert [readiness_band(v) for v in (None, 59, 60, 84, 85)] == [None, "SUPPRESSED", "NORMAL", "NORMAL", "GOOD"]


def test_displayed_readiness_reads_like_a_grade():
    from vitalis.intelligence.readiness import display_score, readiness_label

    # The personal norm (composite 50) shows as 70; order is preserved.
    assert [display_score(v) for v in (None, 20, 50, 56, 81, 100)] == [None, 43, 70, 75, 98, 100]
    assert [readiness_label(v) for v in (59, 60, 74, 75, 84, 85)] == ["偏低", "正常", "正常", "还行", "还行", "较好"]


def test_body_warning_lowers_hard_or_normal_days_only():
    from vitalis.intelligence.contracts import DecisionAction
    from vitalis.intelligence.decision import DecisionEngine

    lower = DecisionEngine._lower_for_body_warning
    assert lower(DecisionAction.TRAIN_HARD, [], True) == (DecisionAction.TRAIN_LIGHT, ["BODY_SIGNAL_WARNING"])
    assert lower(DecisionAction.TRAIN_NORMAL, ["X"], True)[0] == DecisionAction.TRAIN_LIGHT
    assert lower(DecisionAction.TRAIN_LIGHT, [], True) == (DecisionAction.TRAIN_LIGHT, [])
    assert lower(DecisionAction.TRAIN_NORMAL, [], False) == (DecisionAction.TRAIN_NORMAL, [])


def test_morning_key_records_lead_with_readiness():
    from vitalis.intelligence.report_presentation import _daily_metrics

    payload = {"date": "2026-10-14", "features": {
        "recovery": {"readiness_score": 70, "readiness_factors": ["HRV 7 天均值在你 60 天的常态范围内", "静息心率接近你平时"]},
        "sleep": {"duration_minutes": 440},
        "hrv": {"rhr_bpm": 50},
    }}
    metrics = _daily_metrics(payload, morning=True)
    assert metrics[0]["key"] == "readiness" and metrics[0]["value"] == 70
    assert metrics[0]["detail"].startswith("正常（70 是你的平常水平，60 及格）") and "静息心率接近你平时" in metrics[0]["detail"]


def test_wake_charge_is_the_days_maximum_against_the_last_60_days():
    from datetime import datetime, timezone

    from vitalis.intelligence.readiness import wake_charge_summary
    from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint

    day = date(2026, 10, 14)
    points = [
        # History arrives as daily features carrying their maximum.
        SeriesPoint(metric="hybrid_charge", value=60.0, unit="score", day=day - timedelta(days=offset),
                    observed_at=datetime(2026, 9, 1, tzinfo=timezone.utc), source="zepp", source_scope="user_fused",
                    sample_count=900, observed_minutes=900, maximum=float(70 + offset % 20))
        for offset in range(1, 31)
    ] + [
        # The target day is minute samples: charge peaks on waking, then drains.
        SeriesPoint(metric="hybrid_charge", value=value, unit="score", day=day,
                    observed_at=datetime(2026, 10, 14, hour, tzinfo=timezone.utc), source="zepp", source_scope="user_fused")
        for hour, value in ((0, 58.0), (1, 67.0), (3, 61.0))
    ] + [
        # Zepp's own daily summary for the target day is not a wake reading.
        SeriesPoint(metric="hybrid_charge", value=74.0, unit="score", day=day, observed_at=day,
                    source="zepp", source_scope="user_fused")
    ]
    charge = wake_charge_summary(RawDailyProfile(user_id="u", day=day, series={"hybrid_charge": points}))
    assert charge["wake_charge"] == 67.0
    assert charge["wake_charge_reference"] == 77.5
    assert charge["wake_charge_low"] == 73.0 and charge["wake_charge_high"] == 83.0

    few = wake_charge_summary(RawDailyProfile(user_id="u", day=day, series={"hybrid_charge": points[-4:]}))
    assert few["wake_charge"] == 67.0 and few["wake_charge_reference"] is None


def test_morning_readiness_card_names_the_wake_charge():
    from vitalis.intelligence.report_presentation import _daily_metrics

    recovery = {"readiness_score": 40, "readiness_factors": ["静息心率高于你平时"],
                "wake_charge": 67.0, "wake_charge_reference": 81.5, "wake_charge_low": 72.0, "wake_charge_high": 86.0}
    metrics = _daily_metrics({"date": "2026-10-01", "features": {"recovery": recovery}}, morning=True)
    assert metrics[0]["key"] == "readiness"
    assert metrics[0]["detail"].endswith("晨起 Charge 67（你平时 82，偏低）")
    only_charge = {key: value for key, value in recovery.items() if not key.startswith("readiness")}
    metrics = _daily_metrics({"date": "2026-10-01", "features": {"recovery": only_charge}}, morning=True)
    assert metrics[0]["key"] == "wake_charge" and metrics[0]["value"] == 67
    assert metrics[0]["detail"] == "你平时 82，偏低"
