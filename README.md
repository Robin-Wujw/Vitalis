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

公网部署必须在 Vitalis 前提供浏览器信任的 HTTPS 反向代理或隧道，并配置：

```bash
HOST=127.0.0.1
VITALIS_PUBLIC_URL=https://health.example.com
```

临时联调可使用 Cloudflare Quick Tunnel：

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

Quick Tunnel 地址没有稳定性保证；正式部署应使用自有域名和自动续期的 TLS 证书。

## 真实 Zepp 数据接入

Vitalis 通过用户浏览器中的官方登录会话连接 Zepp，不要求打开开发者工具或手工复制 Cookie：

1. 打开 `https://<你的域名>/api/v1/connect/zepp/scan?user=001`。
2. 按页面提示安装 `browser_extension/` 扩展，并在扩展中填写页面显示的 Vitalis 地址和一次性配对码。
3. 点击“登录并自动连接”。扩展会打开 Zepp 官方登录页，可使用页面提供的手机号或账号方式登录。
4. 登录成功后扩展自动读取临时访问凭据并完成配对，Vitalis 随即开始同步数据。

账号密码、短信验证码只提交给官方登录页，Vitalis 不接收也不保存这些字段。

**特性**：
- **自动解析**：从 cookie 自动提取 userid + apptoken + region
- **自动探测区域**：同时探测 15 个 Zepp 区域主机，找可用的那个
- **自动保活**：登录 Cookie 变化时立即更新，并每 30 分钟检查一次浏览器登录状态
- **断联提示**：浏览器退出登录或云端验证失败后，状态 API 返回 `needs_login=true`
- **安全链接**：长期浏览器链接使用高熵 Bearer 令牌，服务端只保存 SHA-256 摘要
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
| GET | `/connect/zepp/scan?user=xxx` | 自动登录配对页 |
| POST | `/connect/zepp/pair` | 创建一次性浏览器配对会话 |
| POST | `/connect/zepp/link/credentials` | 扩展自动更新登录凭据（Bearer） |
| POST | `/connect/zepp/link/disconnected` | 扩展报告浏览器已退出登录 |
| GET | `/connect/zepp/token` | 查询连接、续期和断联状态 |
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

**失效处理**：调用 `/health/token-status` 发现 `needs_login=true` 后，在扩展打开的官方页面重新登录；扩展会自动更新凭据并恢复连接。

## 测试

```bash
.venv/bin/python -m pytest -q        # 75 个测试，全部通过
```

覆盖范围：
- `test_api.py` — 28 个 API 端到端（连接、同步、查询、启动、HTTPS、配对、续期、断联、用户隔离）
- `test_browser_extension.py` — 扩展后台监听、周期检查和无密码输入约束
- `test_fetcher.py` — FetchWindow、心率分页、payload 解析
- `test_health_data_api.py` — 时序指标、每日指标和运动详情查询
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
6. **密码不经过云端**：登录在官方页面完成，Vitalis 只接收登录后的临时访问凭据
