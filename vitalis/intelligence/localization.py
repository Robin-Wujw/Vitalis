"""Chinese presentation labels for stable intelligence codes."""

ACTION_LABELS = {
    "TRAIN_HARD": "高负荷训练",
    "TRAIN_NORMAL": "正常训练",
    "TRAIN_LIGHT": "轻量训练",
    "RECOVERY": "恢复性活动",
    "REST": "休息",
    "INSUFFICIENT_DATA": "数据不足，暂不建议",
}

AVAILABILITY_LABELS = {
    "AVAILABLE": "数据可用",
    "INSUFFICIENT_DATA": "数据不足",
}

QUALITY_LABELS = {
    "SUFFICIENT": "数据完整",
    "PARTIAL": "数据部分缺失",
    "INSUFFICIENT": "数据不足",
}

SIGNAL_LABELS = {
    "sleep_duration": "睡眠时长",
    "hrv": "心率变异性",
}

CONFIDENCE_LABELS = {
    "NONE": "无",
    "LOW": "较低",
    "MODERATE": "中等",
    "HIGH": "较高",
}

INTENSITY_LABELS = {
    "high": "较高强度",
    "moderate": "中等强度",
    "low": "低强度",
    "none": "不训练",
    "undetermined": "暂无法确定",
}

RECOVERY_LABELS = {
    "GOOD": "恢复良好",
    "NORMAL": "状态稳定",
    "SUPPRESSED": "恢复指标偏弱",
    "INSUFFICIENT_DATA": "恢复数据不足",
}

SLEEP_LABELS = {
    "ABOVE_BASELINE": "高于个人基线",
    "NEAR_BASELINE": "接近个人基线",
    "BELOW_BASELINE": "低于个人基线",
    "INSUFFICIENT_DATA": "睡眠数据不足",
}

LOAD_LABELS = {
    "LOW": "近期负荷较低",
    "NORMAL": "近期负荷正常",
    "ELEVATED": "近期负荷偏高",
    "INSUFFICIENT_DATA": "负荷数据不足",
}

SUGGESTED_TYPE_LABELS = {
    "resistance": "力量训练",
    "zone2": "二区有氧跑或等效有氧",
    "walking": "轻松步行",
    "mobility": "关节活动与拉伸",
    "gentle_mobility": "轻柔活动与拉伸",
    "rest": "完全休息",
    "planned_session": "按原计划训练",
}

DRIVER_LABELS = {
    "HRV_ABOVE_BASELINE": "HRV 高于个人基线",
    "HRV_BELOW_BASELINE": "HRV 低于个人基线",
    "RHR_BELOW_BASELINE": "静息心率低于个人基线",
    "RHR_ABOVE_BASELINE": "静息心率高于个人基线",
    "SLEEP_ABOVE_BASELINE": "睡眠高于个人基线",
    "SLEEP_BELOW_BASELINE": "睡眠低于个人基线",
    "SLEEP_SHORT_DURATION": "睡眠时长低于 7 小时保守阈值",
    "TRAINING_LOAD_ELEVATED": "近期训练负荷偏高",
    "TRAINING_LOAD_LOW": "近期训练负荷较低",
    "RECOVERY_NORMAL": "恢复指标处于个人正常范围",
    "PAIN_OR_INJURY_PRESENT": "已记录疼痛或伤病",
    "TRAINING_DAY_UNAVAILABLE": "今天不在设定的可训练日内",
    "HRV_RECENT_7D_BELOW": "近 7 天 HRV 低于前 7 天",
}

LIMITATION_LABELS = {
    "target_day_hrv_missing": "缺少当天 HRV",
    "target_day_sleep_missing": "缺少当天睡眠",
    "target_day_rhr_missing": "缺少当天静息心率",
    "hrv_28d_baseline_insufficient": "HRV 的 28 天基线数据不足",
    "rhr_28d_baseline_insufficient": "静息心率的 28 天基线数据不足",
    "sleep_28d_baseline_insufficient": "睡眠的 28 天基线数据不足",
    "sleep_regularity_history_insufficient": "睡眠规律性历史不足",
    "sleep_stages_are_trend_only": "消费级设备的睡眠分期仅用于趋势参考",
    "training_history_missing": "缺少训练历史",
    "training_load_is_vendor_derived": "训练负荷采用厂商指标",
    "training_load_comparison_insufficient": "缺少完整的前三周训练负荷对照",
    "session_rpe_unavailable": "尚未记录主观用力程度",
    "aerobic_intensity_classification_unavailable": "尚未完成个体化有氧强度分区",
    "fewer_than_two_baseline_interpretable_signals": "可相对基线解释的恢复信号少于两项",
    "vendor_readiness_is_context_only": "厂商准备度仅作为参考信息",
    "vendor_charge_is_context_only": "身体电量仅作为参考信息",
    "multiple_hrv_devices_no_preferred_device_configured": "存在多台 HRV 设备，尚未指定首选设备",
    "multi_device_hrv_disagreement": "多台设备相对各自基线的 HRV 方向不一致",
    "dense_heart_rate_payload_not_decoded": "秒级心率文件已有覆盖索引，但数值载荷尚未完成解码验证",
    "target_night_oxygen_missing": "缺少当晚可解释的血氧数据",
    "oxygen_coverage_insufficient": "当晚血氧覆盖不足",
    "oxygen_is_screening_only": "消费级血氧仅用于趋势观察，不能用于诊断",
    "respiratory_rate_baseline_insufficient": "呼吸频率的 28 天基线数据不足",
}


def labels(values: list[str], mapping: dict[str, str]) -> list[str]:
    return [mapping.get(value, value) for value in values]
