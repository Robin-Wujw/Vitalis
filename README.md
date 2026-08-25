# Vitalis Health Agent

可扩展的个人健康数据平台。

**核心思路：** 数据源插件化采集（Zepp 起步）→ 统一 Vitalis Schema → SQLite/PostgreSQL 存储 → 三层分析引擎（规则 / 统计 / LLM 只解释不计算）→ FastAPI + 多级聚合查询 + 定时推送。

```
                    User / 浏览器 / Agent
                          │
                  FastAPI / Vitalis API
                          │
              ┌───────────┴───────────┐
        Health Intelligence      API Gateway
              └───────────┬───────────┘
                    Vitalis Core
        ┌───────────────┬───────────────┐
   Data Connector   Data Model      Analysis Engine
   (zepp/garmin/…)  (Vitalis Schema) (rule/stat/llm)
        └───────────┬───────────┘
              Sync Manager (6 streams)
        └───────────┴───────────┘
                 Storage Layer
            SQLite → PostgreSQL (+ TimescaleDB)
```

## 项目结构

```
vitalis/
├── connectors/              # 数据源插件（统一接口 authenticate/sync/fetch）
│   └── zepp/
│       ├── client.py        # Zepp 区域云客户端（apptoken 模式）+ Mock
│       ├── fetcher.py       # 数据获取器（7天分块、心率分页、运动翻页）
│       ├── sync_manager.py  # 同步管理器（6条流、逐流报告、超时控制）
│       ├── auth_parser.py   # Cookie 解析器（自动提取 userid/apptoken/region）
│       └── parser.py        # 厂商格式 → Vitalis Schema
├── models/                  # 统一健康数据模型（Vitalis Schema）
├── storage/                 # SQLAlchemy + SQLite/PostgreSQL
├── analysis/                # 三层分析引擎：rule / statistical / ai
├── services/                # 业务服务层
│   ├── sync_service.py      # 旧版同步服务（向后兼容）
│   ├── summary_service.py   # 每日汇总 + 分析
│   ├── aggregation_service.py  # 多级聚合（180d/90d/30d/7d/1d）
│   ├── completeness_service.py # 数据完整性检查
│   └── push_service.py      # 推送服务（日志/Webhook）
├── api/                     # FastAPI 路由（/api/v1）
│   ├── routes/connect.py    # 连接/导入/扫码
│   ├── routes/health.py     # 查询/同步/聚合
│   └── routes/analyze.py    # 分析
├── scheduler/               # 多时段调度（02:00/09:30/14:00）
└── main.py                  # 入口
skills/vitalis/              # Hermes Skill
tests/                       # 单元 + API 端到端测试
```

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env   # 默认 mock 模式，无需凭据
.venv/bin/python -m vitalis.main
```

> 默认 `ZEPP_MOCK=true`：生成确定性模拟数据，端到端流程无需真实 Zepp 凭据即可跑通。

## 真实 Zepp 数据接入

Vitalis 使用 **apptoken 模式**（对齐 [ZeppBridge](https://github.com/lingcang728/ZeppBridge)）：

1. 浏览器打开 `watchface.zepp.com`，**用 Zepp 账号登录**
2. F12 → Application → Cookies → 复制 `hm-user-login-info` 的值
3. 粘贴到 Vitalis 导入页或 API：

```bash
# 网页导入（最简单）
http://<你的服务器IP>:8000/api/v1/connect/zepp/scan?user=001

# 或 API 直接导入
curl -X POST http://localhost:8000/api/v1/connect/zepp/token \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: 001' \
  -d '{
    "cookie": "粘贴完整的 hm-user-login-info 值",
    "sync_history": true,
    "sync_days": 730
  }'
```

**特性**：
- **自动解析**：从 cookie 自动提取 userid + apptoken + region
- **自动探测区域**：同时探测 15 个 Zepp 区域主机，找可用的那个
- **最长 730 天（2 年）**：按 7 天窗口分块，避免单次请求过大
- **6 条数据流逐流报告**：heart_rate → daily_summary → workouts → workout_detail → sleep → hrv

## 自动调度策略

解决「早上 9:30 用户还没起床，数据不完整」的问题：

| 时间 | 动作 | 逻辑 |
|------|------|------|
| **02:00** | 🌙 全量同步 | 补齐最近 7 天历史数据 |
| **09:30** | 🌅 增量同步 + 完整性检查 + 推送 | 检查睡眠是否结束，没醒→推迟 |
| **14:00** | ☀️ 重试推送 | 早上没推送的用户，用已有数据推送 |

每个用户每天只推送一次。

## API（/api/v1）

### 连接与导入

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/connect/zepp/scan?user=xxx` | **网页导入页**（粘贴 cookie） |
| POST | `/connect/zepp/token` | 导入 apptoken（支持完整 cookie 自动解析） |
| GET | `/connect/zepp/token` | 查询 token 状态 |
| POST | `/connect/zepp` | 通用连接入口（最长 730 天） |

### 健康查询

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health/today?day=YYYY-MM-DD` | 今日健康状态 |
| POST | `/health/sync?days=7` | **手动触发增量同步** |
| GET | `/health/token-status` | 凭据有效性 + 下次同步时间 |
| GET | `/health/range?from=&to=&granularity=` | 多级聚合：180d/90d/30d/7d/1d |
| POST | `/analyze` | 完整 AI 分析 |

**示例：**

```bash
# 手动同步（运动后/起床后随时调用）
curl -X POST 'localhost:8000/api/v1/health/sync?days=7' -H 'X-User-Id: 001'

# 检查凭据是否还有效
curl localhost:8000/api/v1/health/token-status -H 'X-User-Id: 001'

# 查看最近 30 天月度聚合
curl 'localhost:8000/api/v1/health/range?from=2026-01-01&to=2026-08-25&granularity=30d' \
  -H 'X-User-Id: 001'
```

## Cookie / apptoken 有效期

| 字段 | 值 | 含义 |
|------|-----|------|
| `ttl` | 31536000 秒 | 登录会话 1 年 |
| `app_ttl` | 3456000 秒 | **apptoken 40 天** |

**实际失效可能更早**：修改密码、其他设备退出登录、服务端风控。

**失效处理**：调用 `/health/token-status` 发现 `valid=false` → 重新登录 watchface.zepp.com 复制新 cookie → 重新导入。

## 测试

```bash
.venv/bin/python -m pytest -q        # 54 个测试，全部通过
```

覆盖范围：
- `test_api.py` — 19 个 API 端到端（连接、同步、查询、聚合、扫码、回调）
- `test_fetcher.py` — FetchWindow、心率分页、payload 解析
- `test_sync_manager.py` — 同步报告、取消、失败报告
- `test_parser.py` — band_data、sport_history 解析
- `test_rule_engine.py` — 规则引擎
- `test_statistical_engine.py` — 统计引擎

## 设计要点

1. **数据源插件化**：`HealthConnector` 抽象 + `register_connector` 注册表
2. **厂商格式隔离**：`connectors/zepp/parser.py` 把 Zepp JSON 转成 Vitalis Schema，上层永远看不到厂商字段
3. **分析逻辑与采集解耦**：连接器只产出 Schema，分析引擎只消费 Schema + 存储
4. **多用户**：`X-User-Id` 请求头 + 表按 user_id 索引 + 调度器按用户逐一同步
5. **LLM 只解释不计算**：rule/statistical 引擎算数字，AIEngine 只打包给 LLM
6. **对齐 ZeppBridge**：端点、请求头、字段映射、翻页逻辑全部对齐 Rust 版实现
# Vitalis
