# 运行、诊断与恢复

[文档导航](README.md) | [快速开始](quickstart.md) | [安全边界](../SECURITY.md)

## 进程与配置

安装包提供 `vitalis serve`（回环 API）、`vitalis worker`（调度器与到期同步分块）、`vitalis db init`（仅空库初始化或校验现有当前 schema）、`vitalis user create`、`vitalis token issue/revoke`、`vitalis doctor`、`vitalis demo` 及受显式确认保护的开发库 `db reset`；`python -m vitalis` 等效。API 与 worker 启动时只校验当前 schema；先显式运行 `vitalis db init`，它们不会自动建库、修复旧库或清理数据。本轮分析输入 revision 列改变了预发布初始基线，旧开发库将被拒绝；需明确使用新的空库，不能自动清理真实用户库。API **不负责启动调度器**。保持一个 worker 作为计划任务所有者；夜间同步时间由 `SYNC_CRON_HOUR` / `SYNC_CRON_MINUTE` 定义，现有晨间单次 09:30、晚间单次 21:30，`SYNC_DISPATCHER_INTERVAL_SECONDS` 控制到期分块检查。只安装 API 而不启动 worker 不会自动同步或投递。所附 [API](../deploy/systemd/vitalis-api.service) 和 [worker](../deploy/systemd/vitalis-worker.service) 单元共用安装路径与私有配置，部署前应验证这些路径、文件权限和服务账号。

`src/vitalis/config.py` 是有效配置键/默认值的代码来源：`DATABASE_URL` 默认本地 SQLite，`VITALIS_TIMEZONE` 默认 `Asia/Shanghai`，`HOST` 默认 `127.0.0.1`，`PORT` 默认 `8000`；`ZEPP_MOCK` 默认 `true`。报告分析、数据覆盖和投递过期边界使用显式的 `VITALIS_TIMEZONE`，不会把服务器操作系统时区当作用户本地日边界。`.env.example` 仅作示例，不能视为验证过的公网配置；PostgreSQL URL 可由配置解析，但其当前部署一致性和恢复路径需单独验证。保存真实 Zepp 凭据必须从数据库外提供有效的 `VITALIS_TOKEN_ENCRYPTION_KEY`，没有密钥时 `ZEPP_MOCK=false` 会在启动时拒绝，已有明文凭据也不会被读取；密钥丢失会阻止解密。`ZEPP_MOCK=true` 时可省略该密钥，但模拟配对凭据仍以不含明文的 `mock-fernet:` 加密格式存储，且该格式只允许模拟模式读取；不能将该模拟密钥策略用于真实模式。内置推送仅在同一私有环境同时设置 `VITALIS_PUSH_USER` 与 `PUSHPLUS_TOKEN` 时才针对绑定用户启用；真实发送需另行授权，不能把测试推送当离线验收。

### A27 durable delivery operations

计划同步在终端事务中写入分析任务；worker 随后执行分析并在同一结果事务中写入通知意图。通知 worker 只读取意图指定的最新成功 run 的已保存 Daily 快照，不会因投递而重新同步或重新分析。资格、日期、过期、睡眠和 coverage/facts-only 门禁由纯 `application/delivery_policy.py` 计算；具体 `adapters/daily_push.py` 负责 PushPlus、直接手动分析和本地 marker。调度路径通过 `scheduled_delivery=True` 禁用 marker，只由 outbox 状态决定重试。`pending` 和有明确拒绝的 `failed` 只按有限次数重试；`succeeded` 表示收到 PushPlus 成功响应，`deferred` 表示投递配置关闭或报告资格/新鲜度门禁不满足，`uncertain` 表示请求超时、进程死亡、上游 5xx 或结果可能已到达 PushPlus，不能自动重发；只有明确且可分类的 4xx 才记为确定拒绝。worker 日志和数据库错误状态只使用有限错误代码，不记录令牌、异常正文或健康内容。

调度投递的幂等键是用户、本地目标日和晨/晚时段；同一天的新分析不会制造第二次发送。未发送的 `pending` / 可重试 `failed` / 指定原因的 `deferred` 意图可通过条件更新改指向新 run，不能覆盖已认领租约。`running` 只能由持有效租约的 worker 在实际发送前核对最新合格 Daily 快照并条件重指向；若刚判定快照不可用、尚未外发时新合格 run 到达，完成事务先锁用户并复查，再安全地恢复 `pending`；如果新 run 在该事务后才提交，分析事务（包括手动分析）也只会唤醒已有的 `deferred/snapshot_unavailable` 意图，不为手动分析新建意图。已经成功或结果不确定的意图不重置；发送前的最新检查仍无法保证远端 exactly-once。数据库备份包含分析任务、快照和通知意图；恢复后先检查 `pending`、`running` 和 `uncertain` 状态再决定是否启动 worker。外部 PushPlus 已接受但本地状态为 `uncertain` 时不得依靠数据库恢复推断 exactly-once，也不得用 `.sent` 文件替代数据库账本。

### systemd 部署模板

两个单元都以非 root 的 `vitalis` 用户和组运行，要求已在 `/opt/vitalis/.venv/bin/vitalis` 安装当前包；`StateDirectory=vitalis` 创建 `/var/lib/vitalis`，工作目录就是该状态目录。管理员须先创建对应的系统账号，并把只对服务账号可读的环境文件放在 `/etc/vitalis/vitalis.env`；该文件至少指定当前 `DATABASE_URL`，真实接入还需提供 Zepp 凭据加密密钥。不要将 `.env.example` 连同空密钥直接作为部署环境，也不要让服务进程以 root 或公网 `HOST` 启动。

把两个单元安装到系统的 systemd 单元目录并运行 `systemctl daemon-reload` 后，先用**同一环境**执行 `vitalis db init` 创建空库，再分别启动 `vitalis-api.service` 与 `vitalis-worker.service`。API 可独立启动，但只启动 API 不会有定时同步；worker 不依赖 HTTP 进程，`GET /live` 只表示进程可达，`GET /ready` 只确认当前 schema；两者都不能证明 worker 在运行或数据已更新。外网访问需要在回环 API 前配置 HTTPS 反向代理及访问控制；真实账号、设备与投递验收不包含在离线 CI 中。Windows 开发按[快速开始](quickstart.md)用 CLI 启动双进程，不使用 systemd。

## 本地 API 令牌

下面只用于**可信本机、已创建的 demo 用户**；先在与 `serve` 相同的私有环境设置 `DATABASE_URL=sqlite:///./demo.db`。CLI 将短期令牌写入**仓库外的全新私有文件**，只将文件路径和非秘密摘要打印到终端；重复使用同一路径会被拒绝。权限按操作选择，不默认授予管理权。

```bash
vitalis token issue --user demo --scope read --expires-days 1 \
  --output "$HOME/.vitalis-demo-token"
export VITALIS_ACCESS_TOKEN="$(< "$HOME/.vitalis-demo-token")"
```

不要将令牌文件、内容或终端环境发送给模型、加入版本库或粘贴到日志。PowerShell 下可用 `$env:VITALIS_ACCESS_TOKEN = (Get-Content -Raw "$HOME/.vitalis-demo-token").Trim()` 在私有终端读取。新建真实用户可执行 `vitalis user create --id <local-user-id>`；CLI 要求当前 schema。令牌仅在数据库存 SHA-256 摘要，操作员可通过 `vitalis token revoke --digest <issued-digest>` 撤销；`doctor` 不验证令牌本身；其 `worker` 字段依据最近心跳显示 `not_seen`、`alive` 或 `stale`，属于近期活动信号，不是实时进程或数据新鲜度保证。API 一般查询需 `read`；分析需 `analyze`，同步需 `sync`，用户反馈需 `feedback`，管理资料/配对需 `manage`。OpenAPI `/docs` 用于校对当前路由和请求参数。

## 同步故障与备份

从有相应权限的客户端查询 `GET /api/data-status` 与 `GET /api/jobs/{job_id}`，分辨 `queued`、`retry_wait`、`partial`、`needs_reauth` 及每个数据流的获取/解析/写入和最近样本时间；同步只通过 `POST /api/sync-jobs` 入队，`days` 限制为 1..730，也可提交明确的 `from`/`to` 本地日期窗口。模拟源同样遵守 1..730 的边界，并按明确的日期窗口生成数据，不会把请求静默截断为固定的 14 天。必要时明确调用 `POST /api/jobs/{job_id}/cancel`；取消的存在性检查和状态变更在同一用户范围事务中完成，跨用户任务统一表现为不存在。`partial` 不表示历史已覆盖；认证拒绝要重新登录，暂时性故障可等待重试。调度器有界处理分块，详情回填按资源预算分批；不要用一次成功解释所有日期或详情已齐。旧日期分析可能与当前健康事件生命周期冲突，不直接批量倒序重算。

### 当前 SQLite 备份与恢复

在私有目录中设置与 API/worker 相同的 `DATABASE_URL`，并选用**尚不存在**的输出文件。备份使用 SQLite 在线 backup API 取得一致快照，运行中的写入无需暂停；命令先校验源库的当前 schema、完整性和外键，再校验生成文件，绝不覆盖现有文件或符号链接。下例的 `private` 目录应预先创建且只允许可信操作员访问：

```bash
export DATABASE_URL="sqlite:///$HOME/private/vitalis.db"
vitalis db backup --output "$HOME/private/vitalis-2026-09-26.sqlite"
```

恢复仅接受现有、独立且符合**当前** schema 的 SQLite 备份；必须显式指定一个**全新**的目标库文件，不能把当前配置库作为目标，也不能将备份直接覆盖到运行中的库。恢复前先停止 API 和 worker，并保留原库及其可能存在的 `-wal`、`-shm` 文件。不要预建目标库；目标文件旁若已有同名 `-wal`、`-shm` 或 `-journal` 文件，命令会拒绝，以免读取旧日志。以下示例恢复到单独路径，人工验证后才修改 API/worker 的 `DATABASE_URL` 并重启：

```bash
vitalis db restore --backup "$HOME/private/vitalis-2026-09-26.sqlite" \
  --database "$HOME/private/vitalis-restored.sqlite"
DATABASE_URL="sqlite:///$HOME/private/vitalis-restored.sqlite" vitalis doctor
```

`doctor` 校验 schema 并读取最近 worker 心跳，但不验证任务结果、厂商凭据可用性或实时进程存活；还应核对恢复库的数据和外部投递标记、同步账本的对应关系，再安全地恢复服务。备份和恢复只复制数据库，不触发实际通知。备份可能包含健康数据及供应商凭据（API 令牌只存摘要），应保持私有并按需要加密保存；启用供应商令牌加密时仍需妥善保管独立的 `VITALIS_TOKEN_ENCRYPTION_KEY`。不支持内存库、非 SQLite URL、未知 schema 或带 sidecar 的备份作为恢复源；这不是迁移工具，也不涵盖 PostgreSQL 恢复或当前基线之外的升级。`db reset` 仅针对明确指定的开发/测试 SQLite 文件，需确认参数，且拒绝当前配置数据库；绝不能拿它修复生产 schema。
