# 按需解释

全程使用中文。回答“为什么”时优先使用 `tools/explain.py` 返回的三级链路：

`facts -> inferences -> action`

趋势问题只使用 TrendResponse，近期变化只使用 HealthEventResponse。仅在字段存在时引用
变化百分比、斜率、偏差或持续天数。多个依据同时存在时，说明这是多信号综合判断。
展示可能改变解释的中文限制。不得把相关性说成因果，也不得根据偏差诊断疾病。回答
训练内容时只能复述 `action.prescriptions`，并使用所有中文标签，不得输出内部英文代码。

训练响应问题只使用 TrainingResponse：明确区分 T+1/T+2/T+3、设备流、缺失窗口与
重叠训练；只有 `recovery_hours` 非空时才说明恢复时长。个人规律只使用 PersonalModel
中的中位数、MAD、样本量、覆盖率和置信度，并称为“关联”或“个人模式”，不得称为
因果。PersonalAssociation 只能复述 `summary`、配对天数、覆盖率、系数、强度、置信度
和限制；必须说明 `association_only=true` 的含义，不得自行筛选阈值、计算显著性或把
弱关联升级为行动建议。时间线只按已返回的 typed summary 说明先后顺序，不得从时间
接近推断因果。
