"""MCP-equivalent descriptive queries over persisted, source-qualified facts."""
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone

from vitalis.intelligence.observation_statistics import goal_comparisons, grouped_observations, heart_rate_minutes, iso
from vitalis.time import local_day, local_day_utc_bounds

from .ports import HealthReader
from .workout_statistics import WorkoutStatistics

BODY_METRICS = ("weight", "height", "bmi", "vendor_bmi", "body_fat_percent", "body_water_percent",
    "bone_mass", "basal_metabolism", "muscle_rate", "muscle_age", "protein_percent", "ideal_weight",
    "visceral_fat_rating", "body_score", "body_style_code", "body_balance_score", "one_foot_measure_time", "impedance", "body_age")

HEALTH_CATEGORIES = {
    "readiness": ("readiness", "physical_readiness", "mental_readiness", "hrv_readiness", "rhr_readiness",
        "skin_temp_readiness", "ahi_readiness", "afib_readiness", "skin_temp_delta", "skin_temp_baseline_delta",
        "skin_temperature_offset_raw", "skin_temperature_baseline_raw", "readiness_status", "sleep_hrv", "sleep_rhr", "hrv_baseline", "rhr_baseline"),
    "hrv": ("sleep_hrv", "hrv_rmssd", "hrv_sdnn"),
    "resting_heart_rate": ("sleep_rhr", "resting_hr"),
    "respiratory_rate": ("respiratory_rate", "respiratory_rate_min", "respiratory_rate_max"),
    "stress": ("stress", "stress_min", "stress_max", "stress_relaxed_pct", "stress_normal_pct",
        "stress_medium_pct", "stress_high_pct", "single_stress"),
    "spo2": ("spo2", "spo2_automatic", "spo2_odi", "spo2_odi_events", "spo2_night_score", "spo2_measured_minutes", "spo2_apnea_low"),
    "pai": ("pai", "pai_total", "pai_daily", "pai_low_zone", "pai_medium_zone", "pai_high_zone", "device_max_hr", "device_resting_hr",
        "pai_low_zone_minutes", "pai_medium_zone_minutes", "pai_high_zone_minutes", "pai_low_zone_lower_limit", "pai_medium_zone_lower_limit", "pai_high_zone_lower_limit"),
    "body_charge": ("hybrid_charge", "mental_charge", "physical_charge"),
    "daily_goals": ("steps", "calories", "active_minutes", "vendor_burning_minutes", "distance_km",
        "step_goal", "calorie_goal", "active_minutes_goal", "running_distance", "cycling_distance"),
}


class HealthAnalytics:
    CATEGORIES = HEALTH_CATEGORIES

    def __init__(self, reader: HealthReader, *, timezone_name="UTC", now_factory=None):
        self._reader = reader
        self._timezone = timezone_name
        self._now = now_factory or (lambda: datetime.now(timezone.utc))

    def _window(self, start, end, basis="calendar_day"):
        if not 0 <= (end - start).days < 730:
            raise ValueError("query requires 1..730 inclusive days")
        return {"from": iso(start), "to": iso(end), "timezone": self._timezone, "time_basis": basis}

    def _bounds(self, start, end):
        return local_day_utc_bounds(start, self._timezone)[0], local_day_utc_bounds(end, self._timezone)[1]

    def _observations(self, user_id, metric, start, end, *, include_samples=False):
        begin, stop = self._bounds(start, end)
        with self._reader.metric_sample_rows(user_id, metric, begin, stop) as rows:
            return grouped_observations((asdict(row) for row in rows), start=start, end=end,
                timezone_name=self._timezone, include_samples=include_samples)

    def metrics(self, user_id, category, start, end, *, include_samples=False):
        window = self._window(start, end)
        if category not in HEALTH_CATEGORIES:
            raise ValueError("unknown health category")
        signals, all_daily = {}, []
        for metric in HEALTH_CATEGORIES[category]:
            daily = self._reader.daily_metrics(user_id, start, end, metric)
            all_daily.extend(asdict(row) for row in daily)
            signals[metric] = {
                "daily": grouped_observations((asdict(row) for row in daily), start=start, end=end,
                    timezone_name=self._timezone, include_samples=True),
                "intraday": self._observations(user_id, metric, start, end, include_samples=include_samples)}
        if category == "resting_heart_rate":
            activity = self._reader.activity_records(user_id, start, end, as_of=self._now())
            observations = [{**row["data"], "date": row["date"], "value": row["data"]["resting_hr"],
                "unit": "bpm", "source_field": "band_data.slp.rhr", "qualification": "vendor_recorded"}
                for row in activity if row["data"].get("resting_hr") is not None and row["data"]["resting_hr"] > 0]
            signals["band_sleep_rhr"] = {"daily": grouped_observations(observations,
                start=start, end=end, timezone_name=self._timezone, include_samples=True), "intraday": []}
        state = "ready" if any(s["daily"] or s["intraday"] for s in signals.values()) else "no_data"
        goals = goal_comparisons(all_daily, start=start, end=end) if category == "daily_goals" else None
        if goals is not None and goals["state"] == "partial":
            state = "partial"
        return {"state": state, "category": category, "range": window, "signals": signals,
                "availability": "saved_data_only", "missing_data": "unknown_not_zero",
                **({"goal_comparisons": goals} if goals is not None else {})}

    def body_composition(self, user_id, start, end):
        window = self._window(start, end)
        begin, stop = self._bounds(start, end)
        grouped, truncated = {}, False
        for metric in BODY_METRICS:
            rows = self._reader.metric_samples(user_id, metric, begin, stop, limit=3001)
            truncated |= len(rows) > 3000
            for row in rows[:3000]:
                key = (iso(row.timestamp), row.source, row.source_scope, row.device_id, row.source_record_id, row.sample_ordinal)
                record = grouped.setdefault(key, {"observed_at": key[0], "source": row.source,
                    "source_scope": row.source_scope, "device_id": row.device_id,
                    "sample_id": row.source_record_id, "sample_ordinal": row.sample_ordinal, "measurements": {}})
                record["measurements"][metric] = {"value": row.value, "unit": row.unit,
                    "source_field": row.source_field, "qualification": row.qualification, "fetched_at": iso(row.fetched_at)}
        records = [grouped[k] for k in sorted(grouped, key=str)]
        for record in records:
            values = record["measurements"]
            record["bmi_consistent"] = True if "bmi" in values else False if values.get("vendor_bmi", {}).get("qualification") == "inconsistent_record" else None
        return {"state": "partial" if truncated else "ready" if records else "no_data", "range": window,
            "records": records, "sample_count": len(records), "truncated": truncated,
            "coverage": {"scope": "holder_records_only", "days_with_data": len({local_day(datetime.fromisoformat(r["observed_at"]), self._timezone) for r in records})},
            "supported_metrics": list(BODY_METRICS)}

    def profile(self, user_id, *, include_devices=True):
        saved = self._reader.source_snapshot(user_id, "profile")
        result = {"state": "ready" if saved and saved["data"] else "no_data", "source": "zepp",
            "qualification": "vendor_profile_not_user_confirmed", "profile": saved["data"] if saved else {},
            "fetched_at": iso(saved["fetched_at"]) if saved else None}
        birth = result["profile"].pop("birth_month", None)
        if birth:
            today = local_day(self._now(), self._timezone)
            year, month = map(int, birth.split("-"))
            age = today.year - year - (today.month < month)
            if 0 <= age <= 130:
                result["profile"].update(age_years=age, age_precision="birth_month", age_as_of=iso(today))
        if include_devices:
            saved_devices = self._reader.source_snapshot(user_id, "devices")
            devices = saved_devices["data"].get("devices", []) if saved_devices else []
            result["devices"] = {"state": "ready" if devices else "no_data",
                "fetched_at": iso(saved_devices["fetched_at"]) if saved_devices else None,
                "items": devices}
        return result

    def daily_summary(self, user_id, start, end, *, include_hourly=False, include_minutes=False):
        window = self._window(start, end)
        if include_minutes and (end - start).days >= 31:
            raise ValueError("minute activity queries require at most 31 days")
        rows = self._reader.activity_records(user_id, start, end, as_of=self._now())
        days = []
        for row in rows:
            data = {**row["data"]}
            band = dict(data.pop("band_activity", {}))
            if not include_minutes:
                band.pop("minute_steps", None)
                band.pop("raw_minutes", None)
            if not include_hourly:
                band.pop("hourly_steps", None)
            days.append({"date": iso(row["date"]), **data, "band_activity": band})
        summaries = {}
        for metric, unit in (("steps", "steps"), ("calories", "kcal"), ("distance_km", "km"), ("active_minutes", "min")):
            summaries[metric] = grouped_observations(({**row, "value": row[metric], "unit": unit,
                "qualification": "vendor_daily_summary"} for row in days if row.get(metric) is not None),
                start=start, end=end, timezone_name=self._timezone)
        goals = self.metrics(user_id, "daily_goals", start, end)
        return {"state": "partial" if goals["state"] == "partial" else "ready" if days else "no_data", "range": window, "days": days,
            "summary": summaries, "sample_count": len(days), "coverage_scope": "saved_source_days",
            "goals": goals}

    def heart_rate(self, user_id, start, end, *, include_series=False):
        window = self._window(start, end)
        groups = self._observations(user_id, "heart_rate", start, end, include_samples=include_series)
        begin, stop = self._bounds(start, end)
        with self._reader.metric_sample_rows(user_id, "heart_rate", begin, stop) as rows:
            minutes = heart_rate_minutes((asdict(row) for row in rows), timezone_name=self._timezone,
                include_series=include_series)
        return {"state": "ready" if groups else "no_data", "range": window, "series": groups,
                "band_minutes": minutes,
                "resting": self.metrics(user_id, "resting_heart_rate", start, end)}

    def sleep(self, user_id, start, end):
        window = self._window(start, end, "sleep_day")
        records = self._reader.sleep_records(user_id, start, end, as_of=self._now())
        daily = defaultdict(lambda: defaultdict(list))
        daily_fields = set(HEALTH_CATEGORIES["readiness"]) | {
            "spo2_odi", "spo2_odi_events", "spo2_night_score", "spo2_measured_minutes", "spo2_apnea_low"}
        if records:
            for row in self._reader.daily_metrics(user_id, start, end):
                if row.metric in daily_fields:
                    daily[row.date][row.metric].append(asdict(row))
        nights, summaries = [], {}
        for record in records:
            data = {**record["data"]}
            enriched = {}
            # Prefer explicit sleep bounds. Time in bed is a different interval.
            stages = data.get("stages") or []
            begin = data.get("sleep_started_at")
            stop = data.get("sleep_ended_at")
            if begin and stop:
                begin = datetime.fromisoformat(begin.replace("Z", "+00:00")) if isinstance(begin, str) else begin
                stop = datetime.fromisoformat(stop.replace("Z", "+00:00")) if isinstance(stop, str) else stop
            elif stages:
                begin = min(datetime.fromisoformat(s["start_time"].replace("Z", "+00:00")) for s in stages)
                stop = max(datetime.fromisoformat(s["end_time"].replace("Z", "+00:00")) for s in stages)
            if begin and stop and begin < stop:
                for metric in ("respiratory_rate", "hrv_rmssd"):
                    rows = self._reader.metric_samples(user_id, metric, begin, stop, limit=10001)
                    enriched[metric] = {"window": {"from": iso(begin), "to": iso(stop)},
                        "groups": grouped_observations((asdict(r) for r in rows[:10000]),
                            start=local_day(begin, self._timezone), end=local_day(stop, self._timezone), timezone_name=self._timezone),
                        "truncated": len(rows) > 10000}
            same_day = {"date": iso(record["date"]), "time_basis": "calendar_day",
                "qualification": "same_report_date_not_sleep_window", "readiness": {}, "overnight_spo2": {}}
            for metric, rows in daily[record["date"]].items():
                category = "readiness" if metric in HEALTH_CATEGORIES["readiness"] else "overnight_spo2"
                same_day[category][metric] = grouped_observations(rows,
                    start=record["date"], end=record["date"], timezone_name=self._timezone, include_samples=True)
            nights.append({"date": iso(record["date"]), **data, "sleep_window_observations": enriched,
                "same_day_observations": same_day})
        for field in ("sleep_duration", "deep_sleep", "light_sleep", "rem_sleep", "awake", "sleep_score",
                      "time_in_bed_minutes", "sleep_efficiency_percent", "wake_count"):
            summaries[field] = grouped_observations(({**n, "value": n[field],
                "unit": "%" if field == "sleep_efficiency_percent" else "score" if field == "sleep_score" else "count" if field == "wake_count" else "min",
                "qualification": "vendor_sleep_record"} for n in nights if n.get(field) is not None),
                start=start, end=end, timezone_name=self._timezone)
        naps = self._reader.nap_records(user_id, start, end, as_of=self._now())
        return {"state": "ready" if nights or naps else "no_data", "range": window, "nights": nights,
                "naps": naps, "summary": summaries, "sample_count": len(nights)}

    def thresholds(self, user_id, start, end):
        window = self._window(start, end, "activity_day")
        metrics = ("lactate_threshold_hr", "lactate_threshold_pace", "vo2max", "training_load",
            "vendor_load_7d", "vendor_load_optimal_min", "vendor_load_optimal_max", "vendor_load_overreaching")
        history = {metric: [{k: iso(v) for k, v in asdict(row).items()}
            for row in self._reader.daily_metrics(user_id, start, end, metric)] for metric in metrics}
        workouts = self._reader.workout_statistics_workouts(user_id, start, end, 10001, timezone_name=self._timezone)
        fallback = []
        missing = 0
        for workout in workouts[:10000]:
            data = workout.data
            if data.get("type") != "running":
                continue
            fields = data.get("vendor_summary_metrics") or {}
            selected = {key: fields[key] for key in ("lactate_threshold_hr", "lactate_threshold_pace", "vo2_max") if key in fields}
            missing += "lactate_threshold_hr" not in selected
            if selected:
                fallback.append({"workout_id": workout.workout_id, "source": workout.source,
                    "observed_at": data.get("started_at"), "fetched_at": data.get("fetched_at"), "measurements": selected})
        current = {}
        for metric in metrics:
            groups = defaultdict(list)
            for row in history[metric]:
                groups[(row["source"], row["source_scope"], row["device_id"], row["unit"],
                        row.get("source_field"), row.get("qualification"))].append(row)
            current[metric] = [max(rows, key=lambda r: r["date"]) for rows in groups.values()]
        return {"state": "partial" if len(workouts) > 10000 else "ready" if any(history.values()) or fallback else "no_data",
            "range": window, "history": history, "current": current, "workout_estimates": fallback,
            "runs_without_estimate": missing, "workouts_truncated": len(workouts) > 10000,
            "selection": "event_history_primary_workout_estimates_separate", "qualification": "vendor_estimate"}

    def period_summary(self, user_id, start, end):
        window = self._window(start, end)
        def section(call):
            try:
                return call()
            except Exception:
                # One unavailable read must not discard already usable sections.
                # Exception text may contain health values or database parameters.
                return {"state": "failed", "failure_code": "section_unavailable",
                        "retryable": True, "next_action": "retry_later"}

        sections = {"activity": section(lambda: self.daily_summary(user_id, start, end)),
            "sleep": section(lambda: self.sleep(user_id, start, end)),
            "training": section(lambda: WorkoutStatistics(self._reader).statistics(user_id, start=start, end=end,
                timezone_name=self._timezone, group_by="sport")),
            "recovery": {name: section(lambda name=name: self.metrics(user_id, name, start, end)) for name in
                ("readiness", "hrv", "resting_heart_rate", "stress", "pai")}}
        flat = [sections[key] for key in ("activity", "sleep", "training")] + list(sections["recovery"].values())
        counts = {state: sum(value["state"] == state for value in flat) for state in ("ready", "partial", "failed", "no_data")}
        state = ("failed" if counts["failed"] == len(flat) else "partial" if counts["failed"] or counts["partial"]
                 else "ready" if counts["ready"] else "no_data")
        return {"state": state, "range": window, **sections, "section_counts": counts,
                "method": "descriptive_saved_observations", "coverage_scope": "each_section_and_source_separate"}
