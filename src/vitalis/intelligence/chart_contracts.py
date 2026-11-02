"""Source-qualified chart values shared by saved reports and read-only APIs."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ChartValue(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")


class ChartPoint(ChartValue):
    timestamp: datetime
    value: float | None = None
    sample_count: int = Field(default=0, ge=0)
    minimum: float | None = None
    maximum: float | None = None

    @field_validator("timestamp")
    @classmethod
    def absolute_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("chart timestamps require an explicit timezone")
        return value.astimezone(timezone.utc)


class ChartRoute(ChartValue):
    id: str
    kind: Literal["device", "account", "unknown", "analysis", "external"]
    label: str
    source: str
    device_id: str | None = None
    device_ids: list[str] = Field(default_factory=list)
    state: Literal["available", "missing", "unknown"] = "missing"
    sample_count: int = Field(default=0, ge=0)
    first_observed_at: datetime | None = None
    last_observed_at: datetime | None = None
    reason: str | None = None


class ChartSeries(ChartValue):
    key: str
    label: str
    unit: str
    source: str
    source_scope: str
    device_id: str | None = None
    device_label: str | None = None
    route_id: str | None = None
    source_field: str | None = None
    qualification: str = "observed"
    aggregation: Literal["observed", "median", "mean", "last", "sum"] = "observed"
    points: list[ChartPoint] = Field(default_factory=list, max_length=3000)
    sample_count: int = Field(default=0, ge=0)
    observed_minutes: int | None = Field(default=None, ge=0)
    first_observed_at: datetime | None = None
    last_observed_at: datetime | None = None
    fetched_at: datetime | None = None


class ChartInterval(ChartValue):
    start: datetime
    end: datetime
    category: str
    label: str
    source: str
    source_scope: str = "unknown"
    device_id: str | None = None
    route_id: str | None = None
    qualification: str = "observed"

    @model_validator(mode="after")
    def time_window(self):
        if self.start.tzinfo is None or self.end.tzinfo is None or self.end <= self.start:
            raise ValueError("chart interval needs an increasing absolute time window")
        return self


class ChartBandPoint(ChartValue):
    timestamp: datetime
    lower: float | None = None
    upper: float | None = None


class ChartBand(ChartValue):
    key: str
    label: str
    source: str
    source_scope: str = "unknown"
    device_id: str | None = None
    route_id: str | None = None
    qualification: str = "vendor_reference"
    points: list[ChartBandPoint] = Field(default_factory=list, max_length=366)


class ChartRegion(ChartValue):
    key: str
    label: str
    direct_sets: float | None = Field(default=None, ge=0)
    indirect_sets: float | None = Field(default=None, ge=0)
    observed_sets: float | None = Field(default=None, ge=0)
    side: str = "unknown"
    qualification: str
    source: str
    source_scope: str = "analysis"
    device_id: str | None = None
    route_id: str | None = None


class ChartReference(ChartValue):
    lower: float | None = None
    upper: float | None = None
    median: float | None = None
    label: str


class ChartSourceSummary(ChartValue):
    text: str
    source: str
    source_scope: str = "unknown"
    device_id: str | None = None
    route_id: str | None = None
    observed_at: datetime | None = None


class Chart(ChartValue):
    key: str
    title: str
    kind: Literal["line", "scatter", "bars", "stages", "body_map"]
    unit: str = ""
    timezone: str
    period_start: datetime
    period_end: datetime
    series: list[ChartSeries] = Field(default_factory=list, max_length=48)
    preview_series: list[ChartSeries] = Field(default_factory=list, max_length=48)
    intervals: list[ChartInterval] = Field(default_factory=list, max_length=3000)
    bands: list[ChartBand] = Field(default_factory=list, max_length=8)
    regions: list[ChartRegion] = Field(default_factory=list, max_length=128)
    notes: list[str] = Field(default_factory=list)
    summary: list[str] = Field(default_factory=list)
    source_summaries: list[ChartSourceSummary] = Field(default_factory=list, max_length=500)
    state: Literal["available", "partial", "unavailable"] = "unavailable"
    resolution_seconds: int | None = Field(default=None, ge=1)
    reference: ChartReference | None = None
    truncated: bool = False
    routes: list[ChartRoute] = Field(default_factory=list, max_length=64)
    selected_route: str = "all"

    @model_validator(mode="after")
    def time_window(self):
        if self.period_start.tzinfo is None or self.period_end.tzinfo is None or self.period_end <= self.period_start:
            raise ValueError("chart needs an increasing absolute time window")
        return self


class ChartBundle(ChartValue):
    schema_version: Literal["1.0"] = "1.0"
    user_id: str
    analysis_run_id: str | None = None
    as_of: datetime
    timezone: str
    period_start: date
    period_end: date
    charts: list[Chart] = Field(default_factory=list, max_length=128)
    notes: list[str] = Field(default_factory=list)
