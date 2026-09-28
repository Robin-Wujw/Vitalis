"""Ported/adapted OpenStrap analytics; Vitalis policy adds hard gates and stale state.

Upstream method: OpenStrap analytics, MIT, revision 45d72ed989c004008b919b366cd5ceda7061b7df.
This module does not claim equivalence with WHOOP or any other vendor.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from math import exp, log
from statistics import median
from typing import Iterable


UPSTREAM_REVISION = "45d72ed989c004008b919b366cd5ceda7061b7df"

METRIC_RANGES: dict[str, tuple[float, float, float]] = {
    "rmssd": (5.0, 250.0, 5.0),
    "hrv_rmssd": (5.0, 250.0, 5.0),
    "rhr": (30.0, 120.0, 2.0),
    "rhr_bpm": (30.0, 120.0, 2.0),
    "resp": (4.0, 40.0, 0.5),
    "respiratory_rate": (4.0, 40.0, 0.5),
}


@dataclass(frozen=True)
class EWMAObservation:
    date: date
    value: float


@dataclass(frozen=True)
class EWMAResult:
    status: str
    tier: str
    accepted: bool
    raw_value: float
    value_used: float | None
    center_before: float | None
    spread_before: float | None
    center_after: float | None
    spread_after: float | None
    winsorized: bool = False
    hard_rejected: bool = False
    stale: bool = False
    history_count: int = 0
    last_date: date | None = None
    reason: str | None = None


def _alpha(half_life: float) -> float:
    return 1.0 - exp(-log(2.0) / half_life)


def _metric_spec(metric: str) -> tuple[float, float, float]:
    key = metric.lower().replace("-", "_")
    if key not in METRIC_RANGES:
        raise ValueError(f"unsupported EWMA metric: {metric}")
    return METRIC_RANGES[key]


def _robust_spread(values: list[float], floor: float) -> float:
    if len(values) < 2:
        return floor
    center = median(values)
    mad = median([abs(value - center) for value in values])
    return max(floor, 1.4826 * mad)


class WinsorizedEWMA:
    """Streaming center/spread EWMA with pre-update target evaluation."""

    def __init__(self, metric: str, *, center_half_life: float = 14, spread_half_life: float = 21):
        self.metric = metric
        self.center_alpha = _alpha(center_half_life)
        self.spread_alpha = _alpha(spread_half_life)
        _, _, self.floor = _metric_spec(metric)
        self.center: float | None = None
        self.spread: float | None = None
        self.history: list[EWMAObservation] = []

    @property
    def count(self) -> int:
        return len(self.history)

    def evaluate(self, value: float, *, observation_date: date | None = None, as_of: date | None = None) -> EWMAResult:
        """Evaluate a target against the old state, then update if accepted."""
        day = observation_date or (self.history[-1].date + timedelta(days=1) if self.history else date.today())
        low, high, floor = _metric_spec(self.metric)
        raw = float(value)
        if not low <= raw <= high:
            return self._refused(raw, day, "OUT_OF_RANGE")

        center = self.center
        spread = self.spread if self.spread is not None else floor
        # OpenStrap warm-up uses the last three nights as its center and a
        # deliberately wider 2.5x uncertainty through night eight.
        eval_center = center
        eval_spread = max(floor, spread)
        if self.count < 8:
            if self.count >= 3:
                eval_center = median(item.value for item in self.history[-3:])
                eval_spread = max(floor, _robust_spread([item.value for item in self.history[-3:]], floor))
            eval_spread *= 2.5
        deviation = abs(raw - eval_center) if eval_center is not None else 0.0
        if center is not None and self.count >= 8 and abs(raw - center) > 5.0 * max(floor, spread):
            return self._refused(raw, day, "HARD_REJECT_GT_5_SPREAD", stale=self._is_stale(as_of, day))

        used = raw
        winsorized = False
        if eval_center is not None and deviation > 3.0 * eval_spread:
            used = eval_center + (3.0 * eval_spread if raw > eval_center else -3.0 * eval_spread)
            winsorized = True

        if center is None:
            new_center = raw
            new_spread = floor
        else:
            center_alpha = _alpha(3.0) if self.count < 8 else self.center_alpha
            new_center = center + center_alpha * (used - center)
            new_spread = max(
                floor,
                spread + self.spread_alpha * (abs(used - center) - spread),
            )
        self.center, self.spread = new_center, new_spread
        self.history.append(EWMAObservation(day, raw))
        stale = self._is_stale(as_of, day)
        status, tier = self._state_label(len(self.history), stale)
        return EWMAResult(
            status=status,
            tier=tier,
            accepted=True,
            raw_value=raw,
            value_used=used,
            center_before=center,
            spread_before=spread,
            center_after=new_center,
            spread_after=new_spread,
            winsorized=winsorized,
            history_count=len(self.history),
            last_date=day,
            stale=stale,
        )

    def update(self, value: float, *, observation_date: date | None = None, as_of: date | None = None) -> EWMAResult:
        return self.evaluate(value, observation_date=observation_date, as_of=as_of)

    def snapshot(self, *, as_of: date | None = None) -> EWMAResult:
        """Return current state without consuming a new observation."""
        if not self.history:
            return EWMAResult(
                status="COLD", tier="cold", accepted=False, raw_value=0.0,
                value_used=None, center_before=None, spread_before=None,
                center_after=None, spread_after=None, history_count=0,
            )
        last_date = self.history[-1].date
        stale = self._is_stale(as_of, last_date)
        status, tier = self._state_label(self.count, stale)
        return EWMAResult(
            status=status, tier=tier, accepted=True, raw_value=self.history[-1].value,
            value_used=self.history[-1].value, center_before=self.center,
            spread_before=self.spread, center_after=self.center, spread_after=self.spread,
            stale=stale, history_count=self.count, last_date=last_date,
        )

    def status_at(self, as_of: date) -> str:
        return self.snapshot(as_of=as_of).status

    def _is_stale(self, as_of: date | None, last_date: date) -> bool:
        return as_of is not None and as_of - last_date > timedelta(days=14)

    @staticmethod
    def _state_label(count: int, stale: bool) -> tuple[str, str]:
        if stale:
            return "STALE", "stale"
        if count >= 14:
            return "TRUSTED", "trusted"
        if count >= 4:
            return "PROVISIONAL", "provisional"
        return "COLD", "cold"

    def _refused(self, raw: float, day: date, reason: str, *, stale: bool = False) -> EWMAResult:
        return EWMAResult(
            status="REFUSED", tier="refused", accepted=False, raw_value=raw,
            value_used=None, center_before=self.center, spread_before=self.spread,
            center_after=self.center, spread_after=self.spread,
            hard_rejected=reason.startswith("HARD_REJECT"), stale=stale,
            history_count=self.count, last_date=self.history[-1].date if self.history else None,
            reason=reason,
        )


def compute_ewma(values: Iterable[EWMAObservation | tuple[date, float]], metric: str, *, as_of: date | None = None) -> list[EWMAResult]:
    stream = WinsorizedEWMA(metric)
    results: list[EWMAResult] = []
    for item in values:
        observation = item if isinstance(item, EWMAObservation) else EWMAObservation(item[0], item[1])
        results.append(stream.evaluate(observation.value, observation_date=observation.date, as_of=as_of))
    if results and as_of is not None and stream.history:
        results[-1] = replace(results[-1], stale=stream.snapshot(as_of=as_of).stale, status=stream.snapshot(as_of=as_of).status, tier=stream.snapshot(as_of=as_of).tier)
    return results


OpenStrapEWMA = WinsorizedEWMA
winsorized_ewma = compute_ewma
openstrap_winsorized_ewma = compute_ewma
