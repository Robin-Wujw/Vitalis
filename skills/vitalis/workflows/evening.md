# 晚间总结

[English](evening.en.md)

若 `open_health_insights.training_load` 有返回，只复述当日 TRIMP 与描述性 ATL/CTL/TSB；TSB
只能称为训练负荷差值，不称为恢复或状态。拒绝或缺输入时简洁说明返回的缺失输入。

全程使用中文，不重复晨间叙述，先说明今天实际发生的内容：

1. 从 `recent_workouts` 使用 `sport_mode_label` 展示当天每次运动、时长、厂商负荷
   和 `recognition_confidence_label`；不得只展示宽泛 `type`。
2. 展示今日负荷、7 日时长、7 日负荷和 `load_state_label`。
3. 使用中文状态、`positive_signal_labels`、`negative_signal_labels` 和动作标签说明
   恢复状态及今晚是否继续活动。
4. 使用可用趋势的 `metric_label`、`direction_label`、`confidence_label`，以及未解决
   事件的 `summary`、严重程度和生命周期标签展示近期背景。
5. 晚间不重新安排当天训练；可回顾 `features.training.running` 和
   `features.training.strength` 的已返回专项分析，但不得自行计算或增加动作。
6. 使用中文依据说明判断，把数据限制固定为最后一节。
7. 训练完成后，可简短询问一次 RPE、身体疲劳、精神状态或肌肉酸痛；收到回答后用
   `tools/feedback.py add` 原样记录，不得替用户推测评分。

当前模型没有恢复预测能力，不得预测明日恢复。
