# 发布与部署 SOP

[文档中心](README.md) | [仓库规则](../AGENTS.md) | [运维与排障](operations.md) | [改动计划](plan/README.md)

本页是预发布环境的唯一发布主责。发布顺序是：**feature branch 改代码 → 本地校验 → 固定候选 SHA 并传到服务器验证 → 更新所有服务并验收 → main 快进到已验收候选并推送 → 清理已合并分支**。服务器地址、用户、私钥和环境文件中的秘密不写入仓库。每次发布从实际 systemd 配置和进程加载路径确定运行版本，不能把服务器 checkout 的 HEAD 当成正在运行的版本。

## 发布原则

- 只部署已经提交、经过本地验证的候选 commit SHA；服务器验收完成前不合并 `main`。
- API、worker、告警检查（`vitalis-alerts.service` 及其 timer）和其它 Vitalis 服务使用同一个 SHA、同一套锁定依赖和一致的数据库、时区配置。
- 本地未提交文件不能复制进运行目录；候选代码使用独立 release 目录，禁止在运行目录直接 `git pull`。
- 只有破坏性迁移才备份数据库（2026-10-13 用户决定）：数据库无变化时保留现库；只加表或加字段时在副本上验证同一组 DDL 后直接对现库执行，并备好反向 DDL；确实需要破坏性变更时备份、验证候选库、切换，并删除被替代的旧工作数据库。
- 服务器只保留当前数据库。破坏性迁移的备份只用于本次发布失败时回滚：切换前生成，验收通过后和 unit/环境回滚副本、更早的备份一起删除，不长期保留（用户已统一授权）。
- 预发布不维护历史 schema/API/report 兼容链。失败先回退代码/环境；加表加字段用反向 DDL 回退，破坏性迁移用校验过的备份回退。
- 真实服务禁止 `ZEPP_MOCK=true`；测试使用隔离的合成数据库、空 PushPlus 凭据，不向真实渠道发送测试通知。
- 不使用 GitHub Actions 等托管 CI。合并前的校验只有第 1 节的本地检查和第 2、6 节的服务器验收，不再添加 workflow。

## 1. 本地修改与候选提交

从仓库根目录、在 feature branch 执行：

```bash
git status --short
uv sync --locked --extra dev
uv run --locked --extra dev python tools/check.py all --ci
uv run --locked --extra dev python tools/generate_report_examples.py --check
git diff --check
```

同时运行受影响目标测试。检查 diff 只含当前目标需要的源码、测试和主责文档，APK、图片、数据库、凭据和日志不进入提交；生成文档无漂移，已删除的旧入口不再被引用。

提交 feature branch 后审阅并记录候选 SHA、源码树和锁文件摘要：

```bash
git diff main...HEAD --stat
git diff main...HEAD --check
git rev-parse HEAD
git rev-parse 'HEAD^{tree}'
```

可推送临时候选分支再让服务器 fetch；也可传输 `git bundle` 或由该提交生成的源码镜像，并校验 SHA 和内容摘要。此时 `main` 保持不变。

## 2. 服务器候选环境与验证

从现有 service unit、drop-in 和进程环境确认当前 release、虚拟环境、环境文件、数据库后端和时区。仅显示允许公开的配置字段；不能打印完整环境文件或进程环境。

以下 `<server>`、`<repo>`、`<candidate_sha>`、`<python>` 和 `<deploy_user>` 由私有部署环境提供；Python 必须在项目支持的 3.11–3.13 范围内。

```bash
ssh <deploy_user>@<server>
git -C <repo> fetch --prune origin
git -C <repo> show --no-patch --format='%H %s' <candidate_sha>
git -C <repo> worktree add --detach /opt/vitalis/releases/<candidate_sha> <candidate_sha>
cd /opt/vitalis/releases/<candidate_sha>
UV_PROJECT_ENVIRONMENT=/opt/vitalis/venvs/<candidate_sha> uv sync --python <python> --locked --extra dev
```

`UV_PROJECT_ENVIRONMENT` 固定实际安装目录；只传 `--python` 不会把依赖装进指定的 release venv。采用源码镜像时也要记录 candidate SHA、源码树、镜像与锁文件摘要和生成时间。

在服务器隔离配置中运行受影响目标测试、生成报告检查、候选 schema 检查和合成 API/worker smoke。本地 `tools/check.py all --ci` 已包含 wheel/sdist 打包验收；资源受限的运行服务器不重复全量 CI 或打包。若需要独立构建检查，先用受支持 Python 的 `ensurepip` 补齐构建工具，并在锁定开发环境验证。测试使用合成数据、空外部凭据和空 PushPlus 配置，临时数据库按服务器资源选择磁盘目录。服务器目标测试或运行验收失败先查明并修复，不合并失败候选。

涉及通知、反馈入口或公开地址配置时，候选验收必须覆盖实际启用的路径：用合成报告和假 HTTP transport 走 `PushService`，分别验证晨报/晚报、HTML/Markdown、有/无反馈链接、最终正文摘要和含链接的运输预算，再模拟业务 `code=200` 与有效流水号。`render_report` 单独成功、`test_delivery` 不带反馈链接，或 API `/ready` 成功，都不能替代这项发送前验收。可运行：

```bash
UV_PROJECT_ENVIRONMENT=<candidate-venv> uv run --locked --extra dev python -m pytest tests/test_push_service.py tests/test_daily_push.py tests/test_feedback_links.py tests/integration/test_notification_outbox.py -q
```

测试进程使用隔离配置，不载入生产凭据或生产数据库；生产开关的组合以显式合成参数重现。

## 3. 停止服务；破坏性迁移才备份

候选验证完成后保存当前 unit/drop-in、release 路径和环境配置的私密回滚副本。确认所有 Vitalis 服务的实际状态，然后停止 API 和 worker，等待进程退出：

```bash
systemctl show vitalis-api vitalis-worker vitalis-alerts.service --property=ActiveState,SubState,MainPID,ExecStart,WorkingDirectory,EnvironmentFiles
sudo systemctl stop vitalis-alerts.timer vitalis-worker vitalis-api
systemctl is-active vitalis-api vitalis-worker vitalis-alerts.timer
```

无 schema 变化或只加表加字段的发布不备份数据库，停服务只为切换，停机一两分钟。只有破坏性迁移（第 4 节第 3 种）才在停服务后备份。SQLite 使用当前版本的备份命令或等价的 WAL-safe 流程，目标必须为新的带 SHA 和时间戳的文件。备份不能位于仓库或 release 目录：

```bash
<old-venv>/bin/vitalis db backup --output /var/backups/vitalis/vitalis-<old_sha>-<timestamp>.sqlite
sha256sum /var/backups/vitalis/vitalis-<old_sha>-<timestamp>.sqlite
```

备份命令必须加载实际服务配置，校验数据库完整性和外键。PostgreSQL 使用 `pg_dump`/`pg_restore` 并保存校验记录；未知后端先确认，不能默认 SQLite。

校验让备份变慢：约 1.4 GB 的库需要 8–9 分钟，期间服务停止，所以安排在没有定时投递的时段。备份只在本次发布期间存在，第 7 节验收通过后删除。

## 4. 数据库变更决策

比较候选代码与实际运行 release 的模型/schema，再验证现库的 schema；不能只依据 checkout HEAD 或本次某个源码文件判断。

1. **无 schema 变化**：保留当前数据库，不备份、不迁移、不删除，通过只读 schema 检查。
2. **只加表或加字段**：不备份。不停服务，先用 SQLite 在线备份 API 把现库拷一份到 `/var/tmp`（约 1.6 GB 时 30 秒），在副本上执行本次明确的 DDL，用候选代码跑 `check_schema` 和真实样例；切换时停服务，对现库在一个事务里执行同一组 DDL、检查涉及表的完整性和外键，再用候选代码 `check_schema`。DDL 用服务用户执行（例如 `systemd-run --uid=vitalis -p EnvironmentFile=…`），免得 root 创建的 WAL/SHM 让服务写不了库。同时写好反向 DDL（删新表、`ALTER TABLE … DROP COLUMN` 删新列、改回 schema revision）备回滚用：旧版本的 schema 检查是严格的，不回退会拒绝启动，新表新列里已写入的数据随回滚丢弃。不建立通用旧版兼容链，验收后删除副本。
3. **确实需要破坏性迁移**：停写并备份旧库，建立当前 schema 候选库，进行明确、可审计的一次性导出/导入。验证 schema、完整性、外键、必要记录保留和合成 API/worker smoke 后切换数据库配置，启动服务并完成第 6 节验收。

破坏性迁移验收成功后，删除已被替代的旧工作数据库及其 WAL/SHM 文件；迁移前的备份按第 7 节一并删除。

数据库清理前记录备份摘要、新 schema revision、导入/丢弃清单、验收结果、部署 SHA、依赖版本和时间戳。使用备份恢复到新路径，不能把删除当作备份。

## 5. 更新所有服务

service unit 可能通过 drop-in 覆盖启动命令，环境文件也可能固定 `PYTHONPATH`；修改前先看实际文件，更新真正生效的配置。`PYTHONPATH` 优先于虚拟环境：只改 `ExecStart` 而环境文件仍指向旧 release 的 `src` 时，进程用新虚拟环境却加载旧代码。API 和 worker 的 `ExecStart` 及源码加载路径同时指向候选版本：

```ini
ExecStart=/opt/vitalis/venvs/<candidate_sha>/bin/vitalis serve
ExecStart=/opt/vitalis/venvs/<candidate_sha>/bin/vitalis worker
ExecStart=/opt/vitalis/venvs/<candidate_sha>/bin/vitalis alerts check
```

告警检查是 oneshot service 加 timer（`deploy/systemd/vitalis-alerts.service`、`vitalis-alerts.timer`），首次部署时从模板安装并 `systemctl enable vitalis-alerts.timer`，之后每次发布和 API、worker 一起切换 `ExecStart`。

如果部署使用 `uvicorn` 启动 API，保留已经验证的绑定地址、端口和日志参数。相对数据库路径按实际 `WorkingDirectory` 解释。用户数据仍位于私密数据目录，环境文件与加密密钥保留在原位置。

```bash
sudo systemctl daemon-reload
sudo systemctl start vitalis-api vitalis-worker vitalis-alerts.timer
systemctl show vitalis-api vitalis-worker vitalis-alerts.service --property=ActiveState,SubState,MainPID,ExecStart
```

检查所有实际 Vitalis 服务，不能只更新某个 Git 仓库或只重启 API。确认进程虚拟环境、工作目录、`PYTHONPATH`、已安装包路径和 release SHA 对齐：从 `/proc/<pid>/environ` 读出 `PYTHONPATH`，并在服务环境下导入 `vitalis` 确认实际加载路径。worker unit 的 `WatchdogSec` 等资源设置以 `deploy/systemd/` 模板为准，改动模板时同步更新服务器上的 unit。

## 6. 运行验收

```bash
curl --noproxy '*' --fail http://127.0.0.1:8000/live
curl --noproxy '*' --fail http://127.0.0.1:8000/ready
<release-venv>/bin/vitalis doctor
systemctl is-active vitalis-api vitalis-worker vitalis-alerts.timer
sudo systemctl start vitalis-alerts.service && systemctl show vitalis-alerts.service --property=Result
```

`doctor` 同样加载实际服务配置。绕过 shell 代理检查回环地址，避免代理 502 被误判成应用错误。继续核对：

- worker heartbeat 在同一个数据库中持续更新；
- API、worker 和其它 Vitalis 服务使用同一候选 SHA、虚拟环境和时区；
- 隔离合成库能生成 morning/daily/evening/weekly/monthly，并通过 API 读取；
- `/api/data-status` 合同正常，能展示同步与覆盖状态；
- 测试没有向真实 PushPlus 发送通知；
- 配置了反馈入口时，完整推送链路的合成受理和最终正文摘要检查通过；实际投递按日期、类型和供应商状态单独记录，未到发送窗口的记为待验证，不能由心跳或合成受理推断真实送达；
- 从日志中仅提取非敏感错误类别和数量，日志不输出凭据、个人信息或健康原文。

保存验收退出码和结果后才进入合并步骤。

## 7. 验收后合并 main 与清理

服务器候选验收成功后，本地重新 fetch，让 `main` 快进到已验收的候选提交（2026-10-13 起，用户决定）：

```bash
git fetch origin --prune
git switch main
git pull --ff-only origin main
git merge --ff-only <candidate_sha>
test "$(git rev-parse HEAD)" = "<candidate_sha>"
git push origin main
```

最终 `main` 的 SHA 就是服务器上运行的候选 SHA，不再产生 merge commit，也不需要为最终 SHA 再构建、切换一次。如果候选分出后 `main` 有了新提交、无法快进，先把 feature branch 变基到最新 `main`，以新的候选 SHA 重新做第 1、2、6 节验收，不能把未验收内容推进 `main`。服务器仓库同步到最终 SHA 后，发布记录与所有服务对齐。

确认服务器与 `origin/main` 对齐后，删除本次候选分支和其它已经合并的临时分支。先检查 `git branch --merged main`、远端合并关系和 worktree 使用状态，只用 `git branch -d` 删除本地已合并分支；不删除仍有独立提交或被其它工作占用的分支。

然后清理服务器，只留当前数据：删除被替代的 release 和虚拟环境、本次及更早的数据库备份（含 `.sha256`）、unit/环境回滚副本（环境副本含密钥）和其它旧库副本；保留当前数据库、运行中的 release 和验收记录。最后在 `/var/backups/vitalis/deployment-<short_sha>-<date>/acceptance.json` 记录 final SHA、服务状态、数据库处理、删除的备份及大小和分支清理结果。

## 8. 失败回滚

任何健康探针、doctor、worker heartbeat、合成报告、真实 schema 检查或实际加载路径验收失败，停止新服务，恢复保存的旧 unit/drop-in 和虚拟环境；加过表或字段时先执行反向 DDL，破坏性迁移则用校验备份恢复到新路径，然后启动旧服务并重新验收。

不要在异常状态下盲目重发 PushPlus，也不合并失败候选。记录失败阶段、退出码、候选 SHA 和下一步，保留备份，恢复并重新验收后再按第 7 节删除；不记录 token、Cookie、数据库内容或健康原文。
