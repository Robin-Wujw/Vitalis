# 按需解释

全程使用中文。回答“为什么”时优先使用 `tools/explain.py` 返回的三级链路：

`facts -> inferences -> action`

趋势问题只使用 TrendResponse，近期变化只使用 HealthEventResponse。仅在字段存在时引用
变化百分比、斜率、偏差或持续天数。多个依据同时存在时，说明这是多信号综合判断。
展示可能改变解释的中文限制。不得把相关性说成因果，也不得根据偏差诊断疾病。回答
训练内容时只能复述 `action.prescriptions`，并使用所有中文标签，不得输出内部英文代码。
