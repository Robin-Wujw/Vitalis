# 发布与部署 SOP

[文档中心](README.md) | [仓库规则](../AGENTS.md) | [运维与排障](operations.md) | [整改计划](plan.md)

本页是预发布环境的唯一发布主责。发布顺序是：**feature branch 改代码 → 本地校验 → 固定候选 SHA 并传到服务器验证 → 更新所有服务并验收 → 合并、推送 main → 核对最终版本并清理已合并分支**。服务器地址、用户、私钥和环境文件中的秘密不写入仓库。每次发布从实际 systemd 配置和进程加载路径确定运行版本，不能把服务器 checkout 的 HEAD 当成正在运行的版本。

## 发布原则

- 只部署已经提交、经过本地验证的候选 commit SHA；服务器验收完成前不合并 `main`。
- API、worker 和其它 Vitalis 服务使用同一个 SHA、同一套锁定依赖和一致的数据库、时区配置。
- 本地未提交文件不能复制进运行目录；候选代码使用独立 release 目录，禁止在运行目录直接 `git pull`。
- 数据库无变化时保留现库；能用加字段解决时不重建；确实需要破坏性变更时备份、验证候选库、切换，并在已有清理授权范围内删除被替代的旧工作数据库，保留回滚备份。
- 预发布不维护历史 schema/API/report 兼容链。失败先回退代码/环境，数据库回退使用校验过的备份。
- 真实服务禁止 `ZEPP_MOCK=true`；测试使用隔离的合成数据库、空 PushPlus 凭据，不向真实渠道发送测试通知。

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

## 3. 停止服务与数据库备份

候选验证完成后保存当前 unit/drop-in、release 路径和环境配置的私密回滚副本。确认所有 Vitalis 服务的实际状态，然后停止 API 和 worker，等待进程退出：

```bash
systemctl show vitalis-api vitalis-worker --property=ActiveState,SubState,MainPID,ExecStart,WorkingDirectory,EnvironmentFiles
sudo systemctl stop vitalis-worker vitalis-api
systemctl is-active vitalis-api vitalis-worker
```

SQLite 使用当前版本的备份命令或等价的 WAL-safe 流程，目标必须为新的带 SHA 和时间戳的文件。备份不能位于仓库或 release 目录：

```bash
<old-venv>/bin/vitalis db backup --output /var/backups/vitalis/vitalis-<old_sha>-<timestamp>.sqlite
sha256sum /var/backups/vitalis/vitalis-<old_sha>-<timestamp>.sqlite
```

备份命令必须加载实际服务配置，校验数据库完整性和外键。PostgreSQL 使用 `pg_dump`/`pg_restore` 并保存校验记录；未知后端先确认，不能默认 SQLite。

## 4. 数据库变更决策

比较候选代码与实际运行 release 的模型/schema，再验证现库的 schema；不能只依据 checkout HEAD 或本次某个源码文件判断。

1. **无 schema 变化**：保留当前数据库，通过只读 schema/完整性检查，不迁移、不删除。
2. **加字段即可**：先备份，在候选副本验证字段、默认值、约束和现有数据；按本次明确的 DDL 更新，验收通过后用于当前库，不建立通用旧版兼容链。
3. **确实需要破坏性迁移**：停写并备份旧库，建立当前 schema 候选库，进行明确、可审计的一次性导出/导入。验证 schema、完整性、外键、必要记录保留和合成 API/worker smoke 后切换数据库配置，启动服务并完成第 6 节验收。

破坏性迁移验收成功后，在清理授权范围内删除已被替代的旧工作数据库及其 WAL/SHM 文件；校验过的原始备份和回滚材料继续保留。已有会话授权不重复询问；没有清理授权时保留旧工作库并说明阻塞。

数据库清理前记录备份摘要、新 schema revision、导入/丢弃清单、验收结果、部署 SHA、依赖版本和时间戳。使用备份恢复到新路径，不能把删除当作备份。

## 5. 更新所有服务

service unit 可能通过 drop-in 覆盖启动命令或 `PYTHONPATH`；修改前先看实际文件，更新真正生效的配置。API 和 worker 的 `ExecStart` 及源码加载路径同时指向候选版本：

```ini
ExecStart=/opt/vitalis/venvs/<candidate_sha>/bin/vitalis serve
ExecStart=/opt/vitalis/venvs/<candidate_sha>/bin/vitalis worker
```

如果部署使用 `uvicorn` 启动 API，保留已经验证的绑定地址、端口和日志参数。相对数据库路径按实际 `WorkingDirectory` 解释。用户数据仍位于私密数据目录，环境文件与加密密钥保留在原位置。

```bash
sudo systemctl daemon-reload
sudo systemctl start vitalis-api vitalis-worker
systemctl show vitalis-api vitalis-worker --property=ActiveState,SubState,MainPID,ExecStart
```

检查所有实际 Vitalis 服务，不能只更新某个 Git 仓库或只重启 API。确认进程虚拟环境、工作目录、`PYTHONPATH`、已安装包路径和 release SHA 对齐。

## 6. 运行验收

```bash
curl --noproxy '*' --fail http://127.0.0.1:8000/live
curl --noproxy '*' --fail http://127.0.0.1:8000/ready
<release-venv>/bin/vitalis doctor
systemctl is-active vitalis-api vitalis-worker
```

`doctor` 同样加载实际服务配置。绕过 shell 代理检查回环地址，避免代理 502 被误判成应用错误。继续核对：

- worker heartbeat 在同一个数据库中持续更新；
- API、worker 和其它 Vitalis 服务使用同一候选 SHA、虚拟环境和时区；
- 隔离合成库能生成 morning/daily/evening/weekly/monthly，并通过 API 读取；
- `/api/data-status` 合同正常，能展示同步与覆盖状态；
- 测试没有向真实 PushPlus 发送通知；
- 从日志中仅提取非敏感错误类别和数量，日志不输出凭据、个人信息或健康原文。

保存验收退出码和结果后才进入合并步骤。

## 7. 验收后合并 main 与清理

服务器候选验收成功后，本地重新 fetch 并确认 `main` 未发生未验证变动，再合并：

```bash
git fetch origin --prune
git switch main
git pull --ff-only origin main
git merge --no-ff <feature-branch>
git diff --exit-code <candidate_sha> HEAD
git rev-parse 'HEAD^{tree}'
git push origin main
git rev-parse HEAD
```

最终 `main` 的源码树必须与服务器验收候选一致；若存在差异，先重新验证，不能把未验收内容混入最终发布。服务器仓库 fast-forward 到最终 `main` SHA，发布记录与所有服务对齐。若 merge commit 产生新 SHA，建立该 SHA 的 release 并校验源码树/锁文件一致，更新全部服务的 release 路径并再次检查探针、doctor 和心跳；或者使用能明确记录 final SHA、candidate SHA 与相同源码树的可校验发布映射。只检查 `git rev-parse HEAD` 不算完成。

确认服务器与 `origin/main` 对齐后，删除本次候选分支和其它已经合并的临时分支。先检查 `git branch --merged main`、远端合并关系和 worktree 使用状态，只用 `git branch -d` 删除本地已合并分支；不删除仍有独立提交或被其它工作占用的分支。最后记录 final SHA、服务状态、数据库处理和分支清理结果。

## 8. 失败回滚

任何健康探针、doctor、worker heartbeat、合成报告、真实 schema 检查或实际加载路径验收失败，停止新服务，恢复保存的旧 unit/drop-in 和虚拟环境；若数据库已切换，使用校验备份恢复到新候选路径，然后启动旧服务并重新验收。

不要在异常状态下盲目重发 PushPlus，也不合并失败候选。记录失败阶段、退出码、候选 SHA 和下一步，保留备份；不记录 token、Cookie、数据库内容或健康原文。
