# Vitalis API

[English](API.en.md)

以下所有路径均以 `/api/v1` 为前缀。用户范围的端点要求显式提供
`X-User-Id` 请求头；不存在隐式的默认用户。健康智能 GET 请求无副作用；
如果请求的快照尚未生成，则返回 `404`。

## 连接与导入

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/connect/zepp/scan?user=...` | 打开浏览器扩展配对页面 |
| POST | `/connect/zepp/pair` | 创建一次性浏览器配对会话 |
| POST | `/connect/zepp/link/credentials` | 通过已认证的浏览器链接更新凭据 |
| POST | `/connect/zepp/link/validate` | 在浏览器 Cookie 暂时不可用时验证已保存的凭据 |
| POST | `/connect/zepp/link/disconnected` | 报告浏览器中的官方登出操作 |
| POST | `/connect/zepp/token` | 导入真实的 Zepp `userid` 和 `apptoken` |
| POST | `/connect/zepp/device-link` | 创建 Balance 2 上传令牌 |
| POST | `/connect/zepp/device-link/heart-rate` | 按样本 ID 结算设备心率批次 |
| GET | `/connect/zepp/token` | 读取连接、续期和重新登录状态 |
| POST | `/connect/zepp` | 连接并同步最多 730 天的数据 |

有关身份认证、安全性和数据源语义，请参阅 [ZEPP_INTEGRATION.md](ZEPP_INTEGRATION.md)。
当 Zepp 厂商身份已属于另一本地用户时，手动导入、初始配对和浏览器链接续期会返回
HTTP `409`；现有凭据及两个用户的历史记录均保持不变。

## 健康智能

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/intelligence/analyze?day=YYYY-MM-DD` | 运行确定性分析并持久化不可变快照 |
| GET | `/intelligence/profile` | 读取带修订版本的用户确认生理和睡眠档案 |
| PATCH | `/intelligence/profile` | 在 `expected_revision` 冲突保护下修补明确指定的档案字段 |
| GET | `/intelligence/daily?day=YYYY-MM-DD` | 读取 DailyProfile 11.0 事实、持久化的决策证据以及仅用于影子计算的开放健康洞察 |
| GET | `/intelligence/weekly?day=YYYY-MM-DD` | 读取 7 天档案及与前一周的比较 |
| GET | `/intelligence/monthly?day=YYYY-MM-DD` | 读取直接计算的 28 天档案 |
| GET | `/intelligence/trends?day=YYYY-MM-DD` | 读取按设备隔离的 7/28/90 天趋势 |
| GET | `/intelligence/events?start=&end=&event_type=` | 读取健康事件生命周期状态 |
| GET | `/intelligence/explain?day=YYYY-MM-DD` | 读取一项已持久化的决策解释，包括快照来源和数据质量 |
| GET | `/intelligence/context?day=YYYY-MM-DD` | 读取有界的 Current/Recent/Trend/Personal 智能体上下文 |
| GET | `/intelligence/training-responses?day=YYYY-MM-DD` | 读取训练后 T+1/T+2/T+3 响应 |
| GET | `/intelligence/personal-model?day=YYYY-MM-DD` | 读取基线、响应分布和受支持的关联 |
| GET | `/intelligence/personal-associations?day=YYYY-MM-DD` | 读取 60/90 天关联评估 |
| GET | `/intelligence/timeline?start=&end=&limit=` | 读取类型化的健康时间线摘要 |
| GET | `/intelligence/recommendations/{id}` | 读取一个建议实例 |
| POST | `/intelligence/recommendations/{id}/complete` | 使用 `workout_source` 和 `workout_id` 关联一项建议 |
| POST | `/intelligence/feedback` | 记录反馈；与训练关联的输入必须提供 `workout_source` 和 `workout_id` |
| GET | `/intelligence/feedback?start=&end=` | 读取主观反馈 |
| GET | `/intelligence/training-preferences` | 读取健康优先的跑步/力量目标、轮换、恶劣天气后备方案和约束条件 |
| PUT | `/intelligence/training-preferences` | 替换完整的训练偏好文档 |
| PATCH | `/intelligence/training-preferences` | 仅更新明确提供的训练偏好字段 |
| POST | `/intelligence/workouts/{workout_id}/strength-exercises?source=` | 替换一项由数据源限定的力量训练中已确认的动作 |
| POST | `/intelligence/events/{id}/acknowledge` | 确认一个用户范围的事件 |

## 健康数据与同步

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/health/sync?days=7` | 创建或复用一次持久的增量同步尝试 |
| GET | `/health/sync/{attempt_id}` | 读取用户范围的尝试、汇总进度、重试和分块结果 |
| POST | `/health/sync/{attempt_id}/cancel` | 持久化取消请求；排队中或已过期的任务会立即结束 |
| GET | `/health/data-health` | 读取最新尝试，以及各数据流的获取、解析、写入、错误和样本新鲜度状态 |
| GET | `/health/token-status` | 读取凭据状态和下次同步时间 |
| GET | `/health/range?from=&to=&granularity=` | 读取 180d/90d/30d/7d/1d 聚合数据块 |
| GET | `/health/workouts?from=&to=` | 列出训练摘要及详细信息的可用性 |
| GET | `/health/workouts/{workout_id}?source=` | 读取当前 v4.0 训练详情及按数据源隔离的类型化样本 |
| GET | `/health/metrics/{metric}?from=&to=&resolution=` | 读取带时间戳的测量值；raw/hour/day 数据点保留数据源、范围、设备和单位 |
| GET | `/health/daily-metrics?metric=&from=&to=` | 读取带数据源来源信息的稀疏每日指标 |
| GET | `/health/dense-files/second_heart_rate?from=&to=` | 读取不含文件 ID 的高频文件覆盖信息 |
| POST | `/health/sync?days=&decode_dense_files=false` | 同步健康数据；除非明确启用单文件解码，否则密集归档仅建立索引 |

`GET /health/metrics/stress?resolution=raw` 返回厂商带时间戳的
`all_day_stress.data` 观测值，其中包含数据源、范围、设备、单位和明确的缺口。
每日平均值、最小值/最大值和类别占比仍可通过
`GET /health/daily-metrics` 单独获取；API 不推导类别阈值，也不填补缺失的
压力区间。

同步尝试使用本地日期窗口，因此重复的等价请求会复用活跃的账本条目，
而不会因请求时间相差数秒而产生差异。公开状态会省略租约令牌、内部阶段和厂商原始错误。
`retry_wait` 保留已完成的分块和下一次重试时间戳；`needs_reauth` 仅用于已分类的
身份认证拒绝；`partial` 包括成功/不可用覆盖混合的情形或调用方明确指定的截止时间。
取消操作具有幂等性，并且在进程重启后仍然有效。手动请求在返回前最多推进
`SYNC_DISPATCHER_BATCH_CHUNKS` 个分块；对于较大的窗口，返回 `queued` 属于正常情况，
由生命周期管理的调度器会继续处理同一次尝试。

## 示例

执行同步，然后显式分析：

```bash
curl -X POST 'http://localhost:8000/api/v1/health/sync?days=7' \
  -H 'X-User-Id: <local-user-id>'

curl -X POST 'http://localhost:8000/api/v1/intelligence/analyze?day=2026-08-28' \
  -H 'X-User-Id: <local-user-id>'
```

存储用户确认的生理输入。这些值优先于训练观测值或设备心率区间候选值：

```bash
curl -X PATCH 'http://localhost:8000/api/v1/intelligence/profile' \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: <local-user-id>' \
  -d '{
    "expected_revision": 0,
    "sex": "MALE",
    "confirmed_hrmax_bpm": 190
  }'
```

读取每日、每周和每月健康智能：

```bash
curl 'http://localhost:8000/api/v1/intelligence/daily?day=2026-08-28' \
  -H 'X-User-Id: <local-user-id>'

curl 'http://localhost:8000/api/v1/intelligence/weekly?day=2026-08-28' \
  -H 'X-User-Id: <local-user-id>'

curl 'http://localhost:8000/api/v1/intelligence/monthly?day=2026-08-28' \
  -H 'X-User-Id: <local-user-id>'
```

记录训练后反馈。Session RPE 必须使用真实的训练 ID：

```bash
curl -X POST 'http://localhost:8000/api/v1/intelligence/feedback' \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: <local-user-id>' \
  -d '{
    "date": "2026-08-28",
    "workout_source": "zepp",
    "workout_id": "<workout-id>",
    "session_rpe": 7,
    "physical_fatigue": 3
  }'
```

确认一次力量训练中的动作。这会替换该训练当前的动作列表，
且不会推断旧记录：

```bash
curl -X POST 'http://localhost:8000/api/v1/intelligence/workouts/<workout-id>/strength-exercises?source=zepp' \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: <local-user-id>' \
  -d '{
    "session_focus": "PUSH",
    "exercises": [{
      "exercise_name": "卧推",
      "sets": 4,
      "repetitions": "8",
      "weight_kg": 60,
      "rir": 2,
      "rest_seconds": 120
    }]
  }'
```

## 契约边界

- `POST /intelligence/analyze` 是计算命令；GET 端点不会运行或更改分析。
  当不存在快照时，`GET /intelligence/explain` 返回 404；Hermes 解释必须报告该状态，
  不得进行同步或分析。
- Daily、Weekly、Monthly、Training Response、Personal Association 和 Personal Model
  快照共享同一个 AnalysisRun 身份。
- 事实、推断和操作在周期档案中保持相互独立。
- `open_health_insights` 仅用于影子计算。它可以解释就绪度、睡眠、TRIMP、ATL、CTL 和 TSB，但不会改变 Decision Policy 7.0、恢复状态、操作、规则 ID 或 ActionPlan。
- 用户确认的档案值具有带修订版本的来源信息。年龄公式、训练最大心率、Zepp 分数和设备心率区间边界绝不会静默填充已确认的 HRmax。
- 缺失的测量值保持为 null，或产生明确的数据不足/拒绝状态。
- 规范训练身份为 `(source, workout_id)`。详情读取、建议完成、反馈、力量训练确认、
  时间线引用和分析输出均保留这两个字段。
- 训练详情样本使用 `metric`、`value` 和 `unit`；已移除的仅心率样本结构
  不会作为别名保留。
- 指标的 `1h` 和 `1d` 聚合绝不会合并不同的数据源、数据源范围、设备或单位数据流。
  每日分桶使用 `VITALIS_TIMEZONE`，而不是 UTC 日历日期；聚合查询会流式读取完整范围，
  而不是应用原始的 50,000 行上限。
- 训练响应重叠身份使用 `source:workout_id`；可比跑步基线
  返回并行的训练数据源数组和 ID 数组。
- 精确的力量训练动作只能来自明确的厂商组数据或用户确认。
  心率可以估算做功/休息结构，但不能确定动作身份或负荷。
- `decision.action_plan` 包含一个主要训练环节，以及至多一个可选的附加项或替代项。
  它包含 7/28 天平衡、安全状态、冲突检查、证据、剂量、停止条件、
  缺失输入门控和本地日期到期时间。已移除的通用训练处方列表字段不会保留。
- 内部枚举代码用于程序控制；中文 `*_label` 字段是展示契约。
- 关联响应属于观察性结果，并始终带有 `association_only=true`。

完整的 Pydantic 模型位于 `vitalis/intelligence/contracts.py`。面向 Hermes 的传输
schema 位于 `skills/vitalis/schemas/`。计算策略记录在
[ARCHITECTURE.md](ARCHITECTURE.md) 中。
