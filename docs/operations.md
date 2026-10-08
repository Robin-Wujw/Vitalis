# 运维、备份与排障

[文档中心](README.md) | [快速开始](quickstart.md) | [安全](../SECURITY.md) | [报告与渠道](reports.md)

本页说明当前预发布环境的配置、进程、备份和故障判断。敏感数据库、健康记录、凭据和投递目标必须使用真实部署的访问控制；示例只使用合成数据。

## 配置主责

默认值只在 `src/vitalis/config.py` 维护：

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| `ZEPP_MOCK` | `true` | 离线 mock；真实 Zepp 必须显式设 `false`。 |
| `VITALIS_ENV` | `dev` | 运行环境标识；`test` 只用于测试。 |
| `DATABASE_URL` | `sqlite:///./vitalis.db` | 当前数据库；演示请改到新路径。 |
| `VITALIS_TIMEZONE` | `Asia/Shanghai` | 本地日历边界和调度时区。 |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | API 监听地址。 |
| `SYNC_CRON_HOUR` / `SYNC_CRON_MINUTE` | `02:00` | worker 自动同步时间。 |
| `VITALIS_WEEKLY_REPORT_ENABLED` | `false` | 周报自动投递开关。 |
| `VITALIS_WEEKLY_REPORT_HOUR` / `MINUTE` | `10:00` | 周报投递本地时间。 |
| `VITALIS_MONTHLY_REPORT_ENABLED` | `false` | 月报自动投递开关。 |
| `VITALIS_MONTHLY_REPORT_HOUR` / `MINUTE` | `10:30` | 月报投递本地时间。 |
| `VITALIS_PUSH_USER` / `PUSHPLUS_TOKEN` | 空 | 单向 PushPlus 目标和令牌；为空时不外发。 |
| `PUSHPLUS_ACCESS_KEY` | 空 | 可选的 PushPlus 状态查询凭据；不参与发送，缺失时保留 `accepted`。 |
| `PUSHPLUS_QUERY_MAX_ATTEMPTS` | `3` | 状态查询最多轮询次数；不会重发原消息。 |
| `PUSHPLUS_QUERY_INTERVAL_SECONDS` | `60` | 状态查询间隔秒数；只影响状态查询。 |

晨报 `09:30`、晚报 `21:30` 的调度由当前 worker 任务定义；配置和时区不要在其它文档复制成另一套默认值。真实模式还需要有效的 `VITALIS_TOKEN_ENCRYPTION_KEY`，并将其与数据库分开保存。

## 初始化与进程

从仓库根目录、指向一个新数据库：

```powershell
$env:DATABASE_URL = 'sqlite:///./vitalis.db'
uv run --locked --extra dev vitalis db init
uv run --locked --extra dev vitalis doctor
```

`db init` 建立当前 schema；`doctor` 输出 schema、数据库后端、时区和 worker 心跳状态，不输出秘密。启动 API 和 worker 应使用相同环境：

```powershell
uv run --locked --extra dev vitalis serve
uv run --locked --extra dev vitalis worker
```

API 的 `/live` 只表示进程存活，`/ready` 检查当前 schema；`doctor` 的 `not_seen` 表示 worker 尚未写心跳，不等于 API 故障。调度器只在 `worker` 进程启动，不能从 API 应用或普通 GET 触发。

## 发布到现有服务

发布前固定提交 SHA，使用只包含 Git 跟踪文件的源码包，在 `/opt/vitalis/releases/<SHA>/source` 建立独立环境。运行服务的配置仍从 `/etc/vitalis/vitalis.env` 加载，用户数据留在 `/var/lib/vitalis`；发布包不携带数据库、凭据或用户提供的 APK/图片。

先在新环境运行锁定依赖安装、合成报告和 `/live`、`/ready` 检查。切换时暂停 API 和 worker，保存原 service 配置与数据库备份，再将执行路径指向新环境。保留原代码和备份，验证失败时先停止新服务再回退；不覆盖私有环境配置。API 保持回环监听和关闭 access log。

当前数据库版本为 `2026-10-durable-pushplus-delivery`。已部署的 `2026-10-calendar-report-delivery` 库可在服务停止后，从新源码目录显式执行：

```bash
ZEPP_MOCK=true python tools/upgrade_deployment_db.py --database /var/lib/vitalis/EXISTING.sqlite --backup /var/backups/vitalis/NEW-backup.sqlite
ZEPP_MOCK=true python tools/upgrade_deployment_db.py --database /var/lib/vitalis/EXISTING.sqlite --backup /var/backups/vitalis/NEW-backup.sqlite --apply
```

第一条仅核验已知结构；第二条创建新备份，在独立候选库升级，验证所有无关记录及原力量记录未改变、投递身份未丢失，再原子替换。未知版本、异常字段和已有备份路径会被拒绝。旧 `succeeded` 仅转为 `accepted`，不虚构流水号或最终送达；旧 `running` 转为 `uncertain`，避免重发未知结果。单位从原 `weight_kg` 保留为 kg，未知计重方式仍为空。运行时依旧只接受当前 schema。

恢复服务后检查 `/ready`、worker 心跳、实际导入路径及服务状态。使用独立临时库运行报告与投递测试，测试配置清空 PushPlus 凭据；不向真实用户试发，不把真实健康内容打印到部署日志。

## 备份与恢复

SQLite 备份必须从当前配置库生成到不存在的新路径：

```powershell
uv run --locked --extra dev vitalis db backup --output C:\secure\vitalis-backup.db
uv run --locked --extra dev vitalis db restore --backup C:\secure\vitalis-backup.db --database C:\secure\vitalis-restored.db
```

命令会检查 schema、完整性、外键和 sidecar；备份、恢复目标和凭据文件不要放入仓库。restore 不覆盖已有文件，也拒绝把备份直接写回当前配置库。恢复后把 `DATABASE_URL` 指向新库，先运行 `doctor`，再启动服务和 worker。不要用文件复制代替带 WAL/SHM 检查的备份。

开发环境若明确要丢弃一个非当前配置的 `.db`/`.sqlite` 文件，必须使用 `db reset --database PATH --confirm-discard-local-data`；生产环境和当前配置库会被拒绝。

## 令牌、同步和投递排障

令牌签发到仓库外的新私密文件：

```powershell
uv run --locked --extra dev vitalis token issue --user demo --scope read --output C:\secure\demo-read.token
```

令牌值不打印到终端或快照。用最小 scope 读取报告；分析、同步、反馈和管理分别需要相应权限。撤销使用非秘密 digest，而不是把令牌放在命令行。

同步排查顺序是：确认来源 token 状态，再查 `GET /api/data-status`，再查 `GET /api/jobs/{job_id}` 的 attempt/chunk，最后查看 worker 日志中的非敏感错误类别。需要重新认证、部分覆盖、超时、取消和未知状态不能互相替代；先保留 `uncertain`，不要盲目重试可能已送达的 PushPlus 请求。

PushPlus 的 HTTP 200 只表示请求完成；只有业务 `code=200` 且有有效 provider message id 才记录 `accepted`。记录 `accepted`、provider message id、最终 `delivered`/`failed`/`uncertain` 分开；投递不会写目标完成或主观反馈。周报/月报开关默认关闭，开启前应使用合成目标做一次受理合同验证；真实渠道和 Hermes 联调当前未声称完成。

## 常见故障

| 现象 | 处理 |
| --- | --- |
| `/ready` 503 | 对照 `DATABASE_URL` 检查 schema；用新库 `db init`，不要自动迁移未知旧库。 |
| worker 无心跳 | 确认独立 worker 进程、同一环境和数据库；查看进程退出原因。 |
| 报告 404 | 指定日期没有已保存快照；创建分析任务并等待 worker，不要把 404 当零数据。 |
| 同步需要认证 | 重新走官方 Zepp 配对；不要在日志或聊天中传 Cookie/apptoken。 |
| 报告延期 | 查看数据覆盖和 required signals；可用已保存事实时只生成合资格内容。 |
| 投递 uncertain | 保留不确定状态。响应丢失且没有流水号时无法自动查询，需在供应商侧人工核对；不创建新意图盲重发。 |
| 投递 failed | 从 `/api/deliveries` 查看 `provider_status`、`send_attempt_count` 和 `next_attempt_at`，结合 worker 的非敏感错误类别排查凭据/模板/目标；明确拒绝的意图由 worker 按退避和最大次数重试。 |

开发检查从仓库根目录运行 `uv run --locked --extra dev python tools/check.py docs`、目标 pytest 和 `git diff --check`。故障报告包含命令、退出码、非敏感状态和未验证项，不包含数据库、健康记录或令牌内容。
