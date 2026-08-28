# Vitalis Health Agent

可扩展的个人健康数据平台。

**核心思路：** 数据源插件化采集（Zepp 起步）→ 统一 Vitalis Schema → 数据质量与来源 → 设备隔离的个人基线 → 睡眠/HRV/恢复/训练特征 → 确定性训练决策 → FastAPI + Hermes 薄渲染。

```
                    User / 浏览器 / Agent
                          │
                  FastAPI / Vitalis API
                          │
                          │
             DailyProfile API (v1.0)
                          │
        Quality → Baseline → Features → Decision
                          │
             Normalized Health Storage
                          │
               Zepp Sync (8 streams)
            SQLite → PostgreSQL (+ TimescaleDB)
```

## 项目结构

```
vitalis/
├── connectors/              # 数据源插件（统一接口 authenticate/sync/fetch）
│   └── zepp/
│       ├── client.py        # Zepp 区域云客户端（apptoken 模式）+ Mock
│       ├── fetcher.py       # 数据获取器（7天分块、心率分页、运动翻页）
│       ├── sync_manager.py  # 同步管理器（8条流、逐流报告、超时控制）
│       ├── auth_parser.py   # Cookie 解析器（自动提取 userid/apptoken/region）
│       └── parser.py        # 厂商格式 → Vitalis Schema + 心率/健康指标解码
├── models/                  # 统一健康数据模型（Vitalis Schema）
├── storage/                 # SQLAlchemy + SQLite/PostgreSQL
├── intelligence/            # DailyProfile、质量、基线、分析器、决策引擎
├── services/                # 业务服务层
│   ├── sync_service.py      # 同步服务
│   ├── aggregation_service.py  # 多级聚合（180d/90d/30d/7d/1d）
│   └── push_service.py      # DailyProfile 渲染与推送（日志/Webhook）
├── api/                     # FastAPI 路由（/api/v1）
│   ├── routes/connect.py    # 连接/导入/扫码
│   ├── routes/health.py     # 查询/同步/聚合
│   └── routes/intelligence.py # 唯一计算分析接口
├── scheduler/               # 同步 + Morning/Evening Profile 推送
└── main.py                  # 入口
skills/vitalis/              # Hermes 薄调度/渲染 Skill
tests/                       # 单元 + API 端到端测试
zepp_os/balance2_bridge/     # Balance 2 设备侧心率补充通道
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
4. 登录成功后扩展从官方页面的一方 Cookie 视图或扩展 Cookie API 自动读取临时访问凭据并完成配对，Vitalis 随即开始同步数据。

账号密码、短信验证码只提交给官方登录页，Vitalis 不接收也不保存这些字段。

**特性**：
- **自动解析**：从扩展 Cookie API 或 Zepp 页面的第一方 Cookie 视图自动提取 userid + apptoken + region，兼容分区 Cookie
- **自动探测区域**：同时探测 15 个 Zepp 区域主机，找可用的那个
- **自动保活**：登录 Cookie 变化时立即更新，并每 30 分钟检查一次浏览器登录状态
- **同步串行化**：同一用户的首次、续期、手动和调度同步共用一把进程内锁，避免 SQLite 并发写冲突
- **可靠断联提示**：只有云端明确拒绝凭据时才返回 `needs_login=true`；Cookie 暂时不可见或网络故障不会误断联
- **安全链接**：长期浏览器链接使用高熵 Bearer 令牌，服务端只保存 SHA-256 摘要
- **最长 730 天（2 年）**：按 7 天窗口分块，避免单次请求过大
- **8 条数据流逐流报告**：heart_rate → daily_summary → workouts → workout_detail → sleep → hrv → wellness → dense_files

### 已实现的数据覆盖

- `band_data.data_hr` 解码为带 UTC 时间戳的分钟心率，最多每天 1,440 个槽位；响应提供设备标识时保留 Balance 2 / Helio Strap 的来源边界。
- SDNN、RMSSD、Charge/身体电量、Readiness（含皮温、睡眠 HRV/RHR、AHI/AFib）、压力、SpO2/ODI、PAI、呼吸率和乳酸阈值进入独立的时序或每日指标表。
- UTC 时间戳按 `VITALIS_TIMEZONE`（默认 `Asia/Shanghai`）切分个人自然日；睡眠时钟使用 Zepp 返回的本地时区偏移，不把 UTC 时钟直接展示给用户。
- `second_heart_rate/real_data` 当前只保存 `SEC_HR` 文件覆盖元数据。文件 ID 不通过健康查询 API 返回，`sample_count=0`、`parse_status=indexed` 明确表示载荷尚未解码。
- 运动摘要保留厂商数字类型 ID；已验证的 Zepp OS `52` (`0x34`) 归一化为力量训练，未知类型仍保留原始 ID 供后续映射审计。
- DailyProfile 保留最近 7 天每次训练的类型、厂商类型 ID、时长、负荷、平均/最大心率和详情可用状态，并提供按类型计数。
- 各事件面按 7 天窗口拉取；API 和同步入口最多接受 730 天。实际覆盖取决于账号创建时间、佩戴情况和厂商保留窗口，空白日期不会被补造。

### 运动期间高频心率

真实 Zepp 运动历史由 `/v1/sport/run/history.json` 返回，记录数组位于
`data.summary`；这个入口会混合多种运动，类型以每条记录的数字 `type` 为准。
运动详情统一通过 `/v1/sport/run/detail.json` 获取，`data.heart_rate` 是“秒数增量、
心率增量”的压缩序列。Vitalis 将它累计解码为 UTC 秒级样本并存入独立的
`workout_samples` 表。

该高频流只覆盖运动详情，不等同于全天高频心率。详情响应没有提供逐样本传感器
身份，因此 API 返回 `source_scope=unknown`、`device_id=null`；即使运动摘要来自
Balance 2，也不能据此把心率样本标记为 Balance 2 或 Helio Strap。当前
`band_data.data_hr` 仍是每天 1,440 个槽位的分钟级数据。

### Balance 2 设备侧补充通道

`zepp_os/balance2_bridge/` 提供 API_LEVEL 4.2 的 Balance 2 Zepp OS 应用骨架。后台服务只监听系统允许的心率回调，使用最多 3,600 条的本地队列，并由手机侧通过 HTTPS + 一次性显示的设备 Bearer 令牌上传。回调频率由 Zepp OS 决定，因此不能宣称固定 1 Hz；高功耗加速度计没有在后台采集。

Helio Strap 不能运行 Zepp OS 应用，所以这条通道仅适用于 Balance 2。云同步仍是历史数据的主来源。设备端配置和构建步骤见 `zepp_os/balance2_bridge/README.md`。

## 自动调度策略

| 时间 | 动作 | 逻辑 |
|------|------|------|
| **02:00** | 夜间同步 | 补齐最近 7 天历史数据 |
| **09:30** | Morning | 增量同步 2 天，生成并推送 DailyProfile |
| **21:30** | Evening | 增量同步 1 天，按晚间视图渲染同一计算结果 |

数据不足不会改用旧分数或模板建议；Profile 和推送都返回
`INSUFFICIENT_DATA` 及缺失信号。

## API（/api/v1）

### 连接与导入

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/connect/zepp/scan?user=xxx` | 自动登录配对页 |
| POST | `/connect/zepp/pair` | 创建一次性浏览器配对会话 |
| POST | `/connect/zepp/link/credentials` | 扩展自动更新登录凭据（Bearer） |
| POST | `/connect/zepp/link/validate` | Cookie 不可见时验证服务端已保存凭据 |
| POST | `/connect/zepp/link/disconnected` | 扩展报告浏览器已退出登录 |
| POST | `/connect/zepp/device-link` | 创建 Balance 2 Zepp OS 上传令牌 |
| POST | `/connect/zepp/device-link/heart-rate` | 接收设备侧心率回调批次（Bearer） |
| GET | `/connect/zepp/token` | 查询连接、续期和断联状态 |
| POST | `/connect/zepp` | 通用连接入口（最长 730 天） |

### 健康查询

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/intelligence/daily-profile?day=YYYY-MM-DD` | v1.0 DailyProfile：质量、事实、基线、特征、状态、决策 |
| POST | `/health/sync?days=7` | **手动触发增量同步** |
| GET | `/health/token-status` | 凭据有效性 + 下次同步时间 |
| GET | `/health/range?from=&to=&granularity=` | 多级聚合：180d/90d/30d/7d/1d |
| GET | `/health/workouts?from=&to=` | 运动摘要列表及详情可用状态 |
| GET | `/health/workouts/{workout_id}` | 运动详情和按时间排序的秒级心率样本 |
| GET | `/health/metrics/{metric}?from=&to=` | 查询通用时序指标和设备来源 |
| GET | `/health/daily-metrics?metric=&from=&to=` | 查询稀疏每日指标 |
| GET | `/health/dense-files/second_heart_rate?from=&to=` | 查询高频文件覆盖，不返回文件 ID |

**示例：**

```bash
# 手动同步（运动后/起床后随时调用）
curl -X POST 'localhost:8000/api/v1/health/sync?days=7' -H 'X-User-Id: 001'

# 检查凭据是否还有效
curl localhost:8000/api/v1/health/token-status -H 'X-User-Id: 001'

# 获取确定性的每日状态与训练决策
curl 'localhost:8000/api/v1/intelligence/daily-profile?day=2026-08-27' \
  -H 'X-User-Id: <local-user-id>'

# 查看最近 30 天月度聚合
curl 'localhost:8000/api/v1/health/range?from=2026-01-01&to=2026-08-25&granularity=30d' \
  -H 'X-User-Id: 001'
```

## Cookie / apptoken 有效期

| 字段 | 值 | 含义 |
|------|-----|------|
| `ttl` | 31536000 秒 | 登录会话 1 年 |
| `app_ttl` | 3456000 秒 | **apptoken 40 天** |

`ttl` 和 `app_ttl` 是响应信息，不是 Vitalis 可使用的官方刷新凭据。扩展只能在官方浏览器会话仍有效且 Cookie 可读取时更新 apptoken；退出登录、Cookie/会话过期、修改密码和厂商风控都可能要求用户重新登录，Vitalis 不会绕过这些边界。

Cookie 暂时不可见时，扩展会调用 `/connect/zepp/link/validate` 验证服务端已保存凭据。临时网络或 Zepp 服务故障返回 503 并保留当前连接；只有云端明确拒绝凭据后才进入 `needs_login`。此时在扩展打开的官方页面重新登录，扩展会自动更新凭据并恢复连接。

## 测试

```bash
.venv/bin/python -m pytest -q        # 117 个测试，全部通过
```

覆盖范围：
- `test_api.py` — API 端到端（连接、同步、查询、配对、续期、设备上传、断联和用户隔离）
- `test_browser_extension.py` — 扩展后台监听、页面一方 Cookie 桥接、周期检查和无密码输入约束
- `test_fetcher.py` — FetchWindow、心率分页、payload 解析
- `test_health_data_api.py` — 时序指标、每日指标、运动详情和秒级样本隔离
- `test_sync_manager.py` — 同步报告、取消、失败报告和运动详情持久化
- `test_parser.py` — band_data、健康指标、文件索引、运动摘要和心率增量解码
- `test_zepp_os_bridge.py` — Balance 2 权限、后台传感器、队列和 HTTPS 上传静态契约
- `test_baseline_engine.py` — 设备/指标隔离、日聚合、7/28 天 robust baseline
- `test_profile_loader.py` — 数据质量、身份隔离和 provenance
- `test_intelligence_analyzers.py` — 睡眠/HRV/恢复/训练与确定性决策
- `test_intelligence_contracts.py` — DailyProfile 版本化契约和无分数兜底语义
- `test_vitalis_skill.py` — Hermes 薄渲染边界、工作流和 Schema

## 设计要点

1. **数据源插件化**：`HealthConnector` 抽象 + `register_connector` 注册表
2. **厂商格式隔离**：`connectors/zepp/parser.py` 把 Zepp JSON 转成 Vitalis Schema，上层永远看不到厂商字段
3. **分析逻辑与采集解耦**：连接器只产出 Schema，分析引擎只消费 Schema + 存储
4. **多用户**：`X-User-Id` 是必填请求头，不存在隐式用户兜底；表按 user_id 索引，调度器按已授权用户逐一同步
5. **LLM 只渲染不计算**：Hermes 只消费 DailyProfile，不生成分数、趋势、阈值或替代建议
6. **密码不经过云端**：登录在官方页面完成，Vitalis 只接收登录后的临时访问凭据
7. **缺失即 abstain**：缺关键目标日信号或可解释基线时输出 `INSUFFICIENT_DATA`
8. **设备流不混合**：RMSSD、SDNN、RHR 及不同设备分别建基线，按可用覆盖选择首选流
