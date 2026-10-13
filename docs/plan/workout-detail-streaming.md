# 训练明细拿全

[改动计划](README.md) · P1

## 为什么

明细已改为先落盘再流式解析，上限 32 MB；9/28 以来因体积被跳过的 70 条跑步明细还缺着，`GET /api/data-status` 的 `workout_details` 显示缺明细数和最近一次体积超限时间。

## 改什么

- 发布后在服务器运行 `vitalis sync details --user <用户>` 补齐，记录最大的真实明细在 worker 内存上限下的耗时和峰值内存。

## 验收

- `workout_details.missing_detail` 为 0（或剩下的有明确的非体积原因），补拉期间同步里不再出现 `resource_limit`。
