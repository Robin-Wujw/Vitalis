"""Build the complete evening report from one already-computed DailyProfile."""
from __future__ import annotations

from typing import Any

from .contracts import ReportBriefing
from .report_formatting import (
    baseline_text,
    date_text,
    energy_label,
    metric_label,
    minutes_text,
    number,
    payload_of,
    repetitions_text,
    timestamp_text,
    unique,
)


class EveningBriefingEngine:
    """Projection only: it does not recalculate metrics or create prescriptions."""

    def build_payload(self, daily: Any, delivery_metadata: dict | None = None) -> dict[str, Any]:
        payload = payload_of(daily)
        context = dict(payload.get("report_context") or {})
        metadata = delivery_metadata or payload.get("delivery_metadata") or {}
        if metadata:
            context["delivery_metadata"] = dict(metadata)
        report_date = payload.get("date")
        features = payload.get("features") or {}
        training = features.get("training") or {}
        sections = self._sections(payload, report_date)
        quality = payload.get("data_quality") or {}
        summary = self._summary(sections, report_date)
        return ReportBriefing(
            period="evening",
            analysis_run_id=payload.get("analysis_run_id", ""),
            user_id=payload.get("user_id", ""),
            date=report_date,
            period_start=report_date,
            period_end=report_date,
            generated_at=payload.get("generated_at"),
            report_context=context,
            data_quality=quality,
            summary=summary,
            sections=[self._section(item) for item in sections],
        ).model_dump(mode="json")

    def build(self, daily: Any, delivery_metadata: dict | None = None) -> ReportBriefing:
        return ReportBriefing.model_validate(self.build_payload(daily, delivery_metadata))

    def _sections(self, payload: dict[str, Any], report_date: Any) -> list[dict[str, Any]]:
        features = payload.get("features") or {}
        training = features.get("training") or {}
        workouts = [
            item for item in training.get("recent_workouts", [])
            if date_text(item.get("date")) == date_text(report_date)
        ]
        running = [
            item for item in (training.get("running") or {}).get("recent_sessions", [])
            if date_text(item.get("date")) == date_text(report_date)
        ]
        strength = [
            item for item in (training.get("strength") or {}).get("recent_sessions", [])
            if date_text(item.get("date")) == date_text(report_date)
        ]
        return [
            self._training_section(workouts, running, strength),
            self._activity_section(payload),
            self._intraday_section(features, payload.get("report_context") or {}),
            self._recovery_section(features, training),
        ]

    @staticmethod
    def _section(value: dict[str, Any]) -> dict[str, Any]:
        return {key: value.get(key, []) for key in ("key", "title", "facts", "interpretation", "limitations")}

    def _training_section(self, workouts: list[dict], running: list[dict], strength: list[dict]) -> dict[str, Any]:
        facts = []
        for workout in workouts:
            label = workout.get("type_label") or workout.get("sport_mode_label") or "训练"
            bits = []
            if workout.get("duration_minutes") is not None:
                bits.append(f"{number(workout['duration_minutes'], 0)} 分钟")
            if workout.get("distance_km") is not None:
                bits.append(f"{number(workout['distance_km'], 2)} 公里")
            if workout.get("calories_kcal") is not None:
                bits.append(f"本次训练估算热量 {number(workout['calories_kcal'])} 千卡")
            if workout.get("heart_rate_avg_bpm") is not None:
                bits.append(f"平均心率 {number(workout['heart_rate_avg_bpm'], 0)} 次/分钟")
            facts.append(f"{label}：" + "；".join(bits or ["已记录，但缺少可显示的专项数值"]) + "。")
        for session in running:
            confidence = session.get("confidence")
            classification = (
                session.get("classification_label", "跑步课型暂不确定")
                if confidence in {"MODERATE", "HIGH"}
                else "跑步课型暂不确定"
            )
            bits = [classification]
            for key, label, digits in (
                ("duration_minutes", "总耗时", 0), ("moving_duration_minutes", "实际运动", 1),
                ("distance_km", "距离", 2), ("average_pace_seconds_per_km", "平均配速", 0),
                ("average_heart_rate_bpm", "平均心率", 0), ("maximum_heart_rate_bpm", "最高心率", 0),
            ):
                value = session.get(key)
                if value is not None:
                    if key == "average_pace_seconds_per_km":
                        value = f"{int(float(value)) // 60}:{int(float(value)) % 60:02d}/公里"
                    elif key in {"average_heart_rate_bpm", "maximum_heart_rate_bpm"}:
                        value = f"{number(value, digits)} 次/分钟"
                    elif key == "distance_km":
                        value = f"{number(value, digits)} 公里"
                    else:
                        value = f"{number(value, digits)} 分钟"
                    bits.append(f"{label} {value}")
            facts.append("跑步专项：" + "；".join(bits) + "。")
            zones = self._zones(session.get("heart_rate_zones"))
            if zones:
                facts.append(f"跑步心率分布：{zones}。")
        for session in strength:
            explicit = session.get("explicit_exercises") or []
            observed = [
                item for item in (session.get("observed_sets") or [])
                if self._has_observed_content(item)
            ]
            known_focus = session.get("focus") not in {None, "UNKNOWN"} and bool(explicit)
            bits = [session.get("focus_label", "力量训练") if known_focus else "力量训练"]
            if session.get("duration_minutes") is not None:
                bits.append(f"{number(session['duration_minutes'], 0)} 分钟")
            if explicit and session.get("total_sets") is not None:
                bits.append(f"明确动作合计 {number(session['total_sets'], 0)} 组")
            elif session.get("vendor_reported_sets") is not None:
                bits.append(f"设备记录 {number(session['vendor_reported_sets'], 0)} 组")
            elif session.get("estimated_work_bouts") is not None:
                bits.append(f"心率结构估计约 {number(session['estimated_work_bouts'], 0)} 个工作段（不是明确组数）")
            if session.get("average_heart_rate_bpm") is not None:
                bits.append(f"平均心率 {number(session['average_heart_rate_bpm'], 0)} 次/分钟")
            facts.append("力量专项：" + "；".join(bits) + "。")
            confirmed = [
                item for item in explicit
                if item.get("source") in {None, "user_confirmed"}
            ]
            if confirmed:
                facts.extend(self._exercise_lines(confirmed))
            elif observed:
                facts.extend(self._observed_set_lines(observed))
            elif explicit:
                facts.extend(self._exercise_lines(explicit))
            else:
                facts.append("力量动作明细：逐组动作、重复次数和重量尚未取得；设备总组数也不能还原这些明细。")
        if not facts:
            facts.append("当天没有已记录的正式训练场次；这不等同于已确认休息日。")
        limitations = []
        if strength and not any(
            item.get("explicit_exercises")
            or any(self._has_observed_content(row) for row in (item.get("observed_sets") or []))
            or item.get("vendor_reported_sets")
            for item in strength
        ):
            limitations.append("心率估计工作段不能替代明确组数，当前不据此评价肌群分配或重量进阶。")
        return {
            "key": "training", "title": "逐场训练", "facts": facts,
            "interpretation": self._training_interpretation(workouts, running, strength),
            "limitations": limitations,
        }

    @staticmethod
    def _has_observed_content(item: dict) -> bool:
        return any(
            item.get(key) not in (None, "")
            for key in (
                "exercise_name", "exercise_id", "vendor_exercise_code", "repetitions",
                "weight_kg", "weight_value", "started_at", "ended_at",
                "duration_seconds", "rest_seconds",
            )
        )

    @classmethod
    def _observed_set_lines(cls, observed_sets: list[dict]) -> list[str]:
        output = []
        for index, item in enumerate(observed_sets, start=1):
            order = item.get("order")
            if not isinstance(order, int) or isinstance(order, bool) or order < 1:
                order = index
            name = item.get("exercise_name") or item.get("exercise_id")
            if not name:
                code = item.get("vendor_exercise_code")
                name = (
                    f"动作代码 {code}（名称未确认）"
                    if code is not None
                    else "动作名称未确认"
                )
            bits = []
            repetitions = repetitions_text(item.get("repetitions"))
            if repetitions:
                bits.append(repetitions)
            else:
                bits.append("次数未记录")
            weight_kg = item.get("weight_kg")
            if isinstance(weight_kg, (int, float)) and not isinstance(weight_kg, bool) and weight_kg >= 0:
                bits.append(f"{number(weight_kg)} 千克")
            else:
                weight_value = item.get("weight_value")
                if isinstance(weight_value, (int, float)) and not isinstance(weight_value, bool) and weight_value >= 0:
                    unit = item.get("weight_unit")
                    if unit:
                        bits.append(f"负重 {number(weight_value)} {unit}")
                    else:
                        bits.append(f"负重 {number(weight_value)}（单位未确认）")
                else:
                    bits.append("负重未记录")
            if item.get("duration_seconds") is not None:
                bits.append(f"用时 {number(item['duration_seconds'], 0)} 秒")
            if item.get("rest_seconds") is not None:
                bits.append(f"休息 {number(item['rest_seconds'], 0)} 秒")
            output.append(f"第 {order} 组：{name}；" + "；".join(bits) + "。")
        return output

    @staticmethod
    def _exercise_lines(exercises: list[dict]) -> list[str]:
        output = []
        for item in exercises:
            bits = []
            if item.get("sets") is not None:
                bits.append(f"{number(item['sets'], 0)} 组")
            repetitions = repetitions_text(item.get("repetitions"))
            if repetitions:
                bits.append(f"每组 {repetitions}")
            if item.get("weight_kg") is not None:
                bits.append(f"{number(item['weight_kg'])} 千克")
            if item.get("rest_seconds") is not None:
                bits.append(f"休息 {number(item['rest_seconds'], 0)} 秒")
            if item.get("rpe") is not None:
                bits.append(f"主观用力 {number(item['rpe'])}/10")
            if item.get("rir") is not None:
                bits.append(f"余力约 {number(item['rir'])} 次")
            output.append(f"动作 {item.get('exercise_name', '已记录动作')}：" + "；".join(bits or ["有动作名称，但剂量未记录"]) + "。")
        return output

    @staticmethod
    def _zones(zones: list[dict] | None) -> str | None:
        if not zones:
            return None
        shares = {int(item.get("zone", 0)): float(item.get("share_percent", 0)) for item in zones}
        return f"低强度 {shares.get(1, 0) + shares.get(2, 0):.1f}%；中等强度 {shares.get(3, 0):.1f}%；阈值附近及以上 {shares.get(4, 0) + shares.get(5, 0):.1f}%"

    @staticmethod
    def _training_interpretation(workouts: list[dict], running: list[dict], strength: list[dict]) -> list[str]:
        if not workouts and not running and not strength:
            return ["没有正式训练记录，不能从空记录推断休息或需要增加训练。"]
        output = []
        if running:
            output.append(f"当天有 {len(running)} 场跑步专项记录，课型和剂量按可用专项明细展示。")
        if strength:
            output.append(f"当天有 {len(strength)} 场力量记录；若动作明细缺失，只解释可验证的时长和汇总信号。")
        return output

    def _activity_section(self, payload: dict[str, Any]) -> dict[str, Any]:
        activity = (payload.get("features") or {}).get("activity") or {}
        facts, interpretation = [], []
        for key, label, unit, digits in (("steps", "步数", "步", 0), ("distance_km", "活动距离", "公里", 2), ("active_minutes", "活动时长", "分钟", 0)):
            metric = activity.get(key)
            value = metric.get("value") if isinstance(metric, dict) else None
            if value is not None:
                facts.append(f"{label} {number(value, digits)} {unit}" + self._metric_baseline(metric) + "。")
                direction = (metric.get("deviation") or {}).get("direction")
                if direction in {"above", "below"}:
                    relation = "高于" if direction == "above" else "低于"
                    interpretation.append(f"本日{label}{relation}自己的近期同口径水平。")
        energies = activity.get("energy") or []
        for item in energies:
            value = item.get("value")
            if value is None or item.get("role") == "workout":
                continue
            facts.append(f"{energy_label(item.get('role'))} {number(value)} 千卡。")
        if not facts:
            facts.append("当天没有可用的步数、距离、活动时长或热量观测。")
        labels = {
            "target_day_activity_missing": "该日期没有可用的日常活动观测。",
            "target_day_incomplete_baseline_comparison_skipped": "本日仍在累计，暂不与完整日总量比较。",
        }
        limitations = [labels.get(value, value) for value in activity.get("limitations") or []]
        if any(item.get("role") == "unspecified" for item in energies):
            limitations.append("未确认口径的热量只作为设备估算热量展示，不与其他热量入口合并；统计范围不足以形成能量平衡判断。")
        return {
            "key": "activity", "title": "日常活动与能量", "facts": facts,
            "interpretation": interpretation,
            "limitations": unique(limitations),
        }

    @staticmethod
    def _metric_baseline(metric: dict | None) -> str:
        if not isinstance(metric, dict):
            return ""
        deviation = metric.get("deviation")
        if deviation:
            return f"（{baseline_text(deviation)}）"
        return ""

    @staticmethod
    def _fact_value(payload: dict[str, Any], metric: str) -> float | None:
        items = (payload.get("facts") or {}).get(metric) or []
        values = [item.get("value") for item in items if isinstance(item.get("value"), (int, float))]
        return float(values[0]) if values else None

    def _intraday_section(self, features: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        activity = features.get("activity") or {}
        facts, limitations = [], []
        stress_labels = {
            "stress": "平均压力", "stress_min": "最低压力", "stress_max": "最高压力",
            "stress_relaxed_pct": "放松区间", "stress_normal_pct": "正常区间",
            "stress_medium_pct": "中等压力区间", "stress_high_pct": "高压力区间",
        }
        summary_parts = []
        for item in activity.get("stress_summary") or []:
            metric = item.get("metric")
            if metric in stress_labels and item.get("value") is not None:
                suffix = "%" if metric.endswith("_pct") else ""
                summary_parts.append(f"{stress_labels[metric]} {number(item['value'])}{suffix}")
        if summary_parts:
            facts.append("设备压力日记录：" + "；".join(summary_parts) + "。")
        for key, label, unit in (("heart_rate", "心率采样", "次/分钟"), ("stress", "压力采样", "")):
            summary = activity.get(key)
            if not summary:
                continue
            bits = []
            if summary.get("sample_count") is not None:
                bits.append(f"{number(summary['sample_count'], 0)} 条记录")
            if summary.get("observed_minutes") is not None:
                bits.append(f"分布于 {number(summary['observed_minutes'], 0)} 个有记录的分钟")
            for field, name in (("first_observed_at", "首条"), ("last_observed_at", "末条")):
                if summary.get(field) is not None:
                    bits.append(f"{name} {timestamp_text(summary[field], context.get('timezone'), short=True)}")
            for field, name in (("minimum", "最低"), ("maximum", "最高"), ("average", "记录均值")):
                if summary.get(field) is not None:
                    bits.append(f"{name} {number(summary[field])}{(' ' + unit) if unit else ''}")
            facts.append(f"{label}：" + "；".join(bits) + "。")
            limitations.extend(summary.get("limitations") or [])
            if summary.get("truncated"):
                limitations.append(f"{label}查询未覆盖全部记录，不能据此描述完整日。")
        if not facts:
            facts.append("没有可用的日内心率或压力观测。")
        else:
            limitations.append("有记录的分钟和首末时刻不代表连续监测时长；这些统计仅描述已采样时段。")
        return {
            "key": "intraday", "title": "日内心率与压力覆盖", "facts": facts,
            "interpretation": [], "limitations": unique(limitations),
        }

    def _recovery_section(self, features: dict[str, Any], training: dict[str, Any]) -> dict[str, Any]:
        sleep = features.get("sleep") or {}
        hrv = features.get("hrv") or {}
        recovery = features.get("recovery") or {}
        facts = []
        if sleep.get("duration_minutes") is not None:
            facts.append(f"昨夜睡眠时长 {minutes_text(sleep['duration_minutes'])}，这是前一晚恢复事实，不是今天训练后的测量。")
        if hrv.get("value_ms") is not None:
            facts.append(f"{metric_label(hrv.get('preferred_metric'))} {number(hrv['value_ms'])} 毫秒，{baseline_text(hrv.get('deviation'))}。")
        if hrv.get("rhr_bpm") is not None:
            facts.append(f"{metric_label(hrv.get('rhr_metric') or 'resting_hr')} {number(hrv['rhr_bpm'])} 次/分钟，{baseline_text(hrv.get('rhr_deviation'))}。")
        if hrv.get("recent_7d_median_ms") is not None:
            label = metric_label(hrv.get("preferred_metric"))
            recent = f"近 7 日{label}中位数 {number(hrv['recent_7d_median_ms'])} 毫秒"
            if hrv.get("previous_7d_median_ms") is not None:
                recent += f"；前 7 日 {number(hrv['previous_7d_median_ms'])} 毫秒"
            if hrv.get("recent_7d_change_percent") is not None:
                recent += f"，变化 {hrv['recent_7d_change_percent']:+.1f}%"
            facts.append(recent + "。")
        state = recovery.get("state_label")
        interpretation = [f"已有恢复信号的综合判定：{state}。"] if state else []
        if recovery.get("positive_signal_labels"):
            interpretation.append("相对有利的信号：" + "；".join(recovery["positive_signal_labels"]) + "。")
        if recovery.get("negative_signal_labels"):
            interpretation.append("需要留意的信号：" + "；".join(recovery["negative_signal_labels"]) + "。")
        burden = []
        if training.get("today_duration_minutes") is not None:
            burden.append(f"训练时长 {number(training['today_duration_minutes'], 0)} 分钟")
        if training.get("today_load") is not None:
            burden.append(f"训练负荷 {number(training['today_load'])}")
        if burden:
            facts.append("当天已记录" + "、".join(burden) + "。")
        if training.get("load_7d") is not None:
            load = f"近 7 日设备训练负荷 {number(training['load_7d'])}"
            if training.get("load_7d_reference") is not None:
                load += f"；此前 3 周的周均参照 {number(training['load_7d_reference'])}"
            if training.get("load_7d_change_percent") is not None:
                load += f"，变化 {training['load_7d_change_percent']:+.1f}%"
            facts.append(load + "。")
        if hrv.get("corroboration_affects_decision"):
            interpretation.append("HRV 流之间存在分歧，有限解读以综合状态和其他有效信号为准。")
        limitations = ["恢复背景不等于运动结束后的恢复实测，训练负荷变化也不直接代表训练效果。"] if burden else []
        return {"key": "recovery", "title": "恢复背景与当天训练", "facts": facts or ["恢复背景数据不可用。"], "interpretation": interpretation, "limitations": limitations}

    @staticmethod
    def _summary(sections: list[dict[str, Any]], report_date: Any) -> list[str]:
        return [f"{date_text(report_date)} 的已记录事实：{sections[0]['facts'][0]}", sections[1]["facts"][0], (sections[3]["interpretation"] or ["恢复背景信息不足，暂不作综合判断。"]) [0]]
