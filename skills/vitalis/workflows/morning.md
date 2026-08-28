# 晨间分析

Use the requested date, or today's date when none is given.

全程使用中文，按以下顺序渲染：

1. 仅在当天 `features.sleep.status` 为 `AVAILABLE` 且 `wake_time` 存在时推送；
   否则延后重试，不得改用昨天的数据。
2. 使用 `decision.action_label`、`states.recovery_label` 和
   `decision.confidence_label` 展示综合结论。
3. 展示睡眠时长及可用的 28 天偏差。
4. 展示所选 HRV 指标的每个当天设备流及其 28 天偏差，并标出所选流；仅在有值时
   展示静息心率（RHR）。不得暗示确定性选择代表该设备更准确。
5. 使用 `positive_signal_labels` 和 `negative_signal_labels` 解释恢复判断。
6. 使用 `suggested_type_labels`、`intensity_label`、时长和
   `prescription_guidance` 展示建议，再逐项完整渲染 `prescriptions` 的目标、步骤、
   组次、休息、进阶和注意事项。
7. 使用 `driver_labels` 展示依据，并把 `limitation_labels` 作为最后一节。

当内部动作是 `INSUFFICIENT_DATA` 时，仅使用 `data_quality.status_label`、
`missing_required_signal_labels` 和中文限制说明，不得用通用训练建议代替缺失结论。
