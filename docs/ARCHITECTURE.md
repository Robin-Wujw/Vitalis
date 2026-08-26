# Vitalis Health Agent — Architecture v1.0

## 1. 总体架构

```
                    User
                     |
        -------------+-------------
        |                           |
 Official Login + Extension   Hermes Agent + Skill
        |                           |
        -------------+-------------
                     |
        ------------------------
        |                      |
  Health Intelligence      API Gateway
        |                      |
        ------------------------
                     |
             Vitalis Core
                     |
     --------------------------------
     |              |               |
 Data Connector  Data Model   Analysis Engine
     |              |               |
     |              |               |
  Zepp          HealthSchema     Rule Engine
  Garmin                         Statistical
  Apple                          AI (LLM解释)
  Huawei
                     |
                Storage Layer

          PostgreSQL + TimescaleDB
```

## 2. 核心模块

### 2.1 Data Connector Layer（vitalis/connectors）

统一接口（`base.py`）：

```python
class HealthConnector(ABC):
    source: str                     # "zepp" / "garmin" ...
    def authenticate(self) -> ConnectorAuth
    def sync(self, user, start, end) -> ConnectorSyncResult   # 拉取->转换->入库
    def fetch(self, user, start, end) -> list[DailyHealth]    # 只拉取+转换（预览）
```

- 厂商格式隔离：`connectors/zepp/parser.py` 把 Zepp 字段（`sleepScore`/`stages`…）转换为统一 Schema。
- 插件注册：`@register_connector`（见 `registry.py`），业务层只依赖抽象。

### 2.2 Health Data Model（vitalis/models）

**原则：不保存厂商格式。** 所有数据进入系统前一律转换为 Vitalis Schema。
单位统一：时长=分钟、心率=bpm、负荷=0~100。

核心类型：`User / Device / SleepRecord / ActivityRecord / TrainingRecord / Workout / DailyHealth / AnalysisRecord / Decision`（见 `models/models.py`）。

### 2.3 Storage（vitalis/storage）

- 初期 PostgreSQL（psycopg），开发/测试可切 SQLite（`DATABASE_URL`）。
- 表：`users`、`devices`、`health_daily`、`sleep_records`、`activity_records`、`training_records`、`workouts`、`workout_samples`、`analysis_records`、`auth_tokens`、`zepp_pairing_sessions`、`zepp_browser_links`。
- 健康数据以 JSON 列存统一 Schema，同时保留结构化列；ORM 已为 TimescaleDB 超表迁移预留（`__table_args__`）。
- Zepp 运动摘要从真实 `data.summary` 响应归一化；详情中的累积心率增量被展开为 UTC 秒级 `workout_samples`。同一运动重新同步时整组替换，避免重复或残留样本。
- 运动详情没有提供逐样本设备身份时，样本来源必须保持 `unknown`，不得根据运动摘要设备推断为 Balance 2、Helio Strap 或融合数据。
- Zepp 访问凭据加密存储；长期浏览器链接只保存 Bearer 令牌的 SHA-256 摘要及连接状态，不保存原始链接令牌。

### 2.4 Analysis Engine（vitalis/analysis）

三层，严格分层、可独立测试：

| 层 | 模块 | 职责 | 特性 |
| --- | --- | --- | --- |
| Rule | `rule_engine.py` | 确定性规则：睡眠<6h→恢复下降、负荷过高→不建议高强度 | 可复现、可解释 |
| Statistical | `statistical_engine.py` | 7 天 HRV 趋势、30 天训练负荷、7 天平均睡眠 | 纯函数 |
| AI | `ai_engine.py` | 只解释/总结/建议，**绝不计算** | LLM 或确定性模板回退 |

编排入口 `AnalysisPipeline.run(target, history)`。

### 2.5 Agent Interface（skills/vitalis）

Hermes Skill：`SKILL.md` 声明能力（查询/分析/建议）与规则（不医疗诊断、基于趋势、LLM 不计算），`tools/` 提供 CLI（走 HTTP API）。

## 3. API 设计（/api/v1）

- `POST /connect/zepp` — 连接数据源、同步历史、生成健康档案
- `POST /connect/zepp/pair` — 创建绑定 Vitalis 用户的短期一次性配对码
- `POST /connect/zepp/pair/{code}/credentials` — 扩展提交官方网页登录结果，签发长期浏览器链接并开始同步
- `POST /connect/zepp/link/credentials` — Cookie 变化或周期检查时验证并更新访问凭据
- `POST /connect/zepp/link/disconnected` — 记录浏览器退出登录，向用户暴露重登提示
- `GET /connect/zepp/token` — 查询凭据、最近验证/同步时间和 `needs_login` 状态
- `GET /health/today` — 今日状态摘要 `{score, sleep, training, stress}`
- `GET /health/workouts` — 按日期查询运动摘要和详情可用状态
- `GET /health/workouts/{workout_id}` — 查询归一化详情及秒级运动心率样本
- `POST /analyze` — 完整分析（规则+统计+LLM 解释）
- 多用户：`X-User-Id` 请求头

## 4. 用户数据流程

```
公网：浏览器 → 受信任 HTTPS 反向代理 / 隧道 → 回环地址上的 FastAPI
首次：用户 → Vitalis 配对页 → 扩展打开 Zepp 官方登录页
      → 手机号/账号登录 → 扩展检测 Cookie → 一次性配对码提交
      → 保存 auth_tokens + 签发浏览器链接 → 同步历史 → 生成健康档案
续期：Cookie 变化事件或 30 分钟检查 → Bearer 浏览器链接验证
      → 凭据变化时换存并增量同步；退出登录时标记 needs_login
每日：定时 sync job（用已保存 token）→ 更新数据/连接状态 → Hermes 可调用
运动：`run/history.json` 的 `data.summary` → 运动摘要
      → `run/detail.json` 的压缩增量 → UTC 秒级 `workout_samples`
```

## 5. 与任务书的对应

1. 数据源插件化 ✅ Connector 抽象 + 注册表
2. 分析逻辑与采集解耦 ✅ 只通过 Schema/仓储交互
3. 多用户 ✅ X-User-Id + 调度循环 + 每用户独立 token
4. 支持未来第三方调用 ✅ 独立 FastAPI + TimescaleDB 迁移预留
5. LLM 只负责解释不负责计算 ✅ AIEngine 只消费结构化事实

## 6. 后续路线

- Garmin / Apple / Huawei 连接器（各自实现 parser 即可）
- PostgreSQL + TimescaleDB 生产配置与迁移
- 分析规则可配置化（阈值可通过配置/表注入）
- 多租户与访问令牌体系
