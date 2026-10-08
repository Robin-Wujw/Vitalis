# 发布与部署 SOP

[文档中心](README.md) | [仓库规则](../AGENTS.md) | [运维与排障](operations.md) | [整改计划](plan.md)

本页是预发布环境的唯一发布主责。仓库当前没有自动 SSH 发布脚本，也没有 Docker/Compose 部署入口；以下步骤由具备服务器权限的维护者执行。服务器地址、用户、私钥和 `/etc/vitalis/vitalis.env` 不写入仓库。

## 发布原则

- 只部署已经在本地验证过的 `main` commit SHA。
- 服务器不能直接运行未提交的工作区或自行选择分支。
- API 和 worker 必须同时切换到同一 SHA、同一虚拟环境和同一配置。
- 预发布允许破坏性更新，不保留旧版 schema/API/report 兼容链。
- 失败时先停止新服务并回退代码/环境；数据库回退使用备份，不运行旧迁移脚本。
- 真实部署禁止使用 `ZEPP_MOCK=true`，除非明确进入隔离演示环境；PushPlus 测试使用空凭据和合成数据库。

## 1. 本地完成和合并 main

从仓库根目录执行：

```bash
git status --short
uv sync --locked --extra dev
uv run --locked --extra dev python tools/check.py all --ci
uv run --locked --extra dev python tools/generate_report_examples.py --check
git diff --check
```

再运行受影响的目标测试。确认：

- 没有真实数据库、令牌、Cookie、APK、图片或日志进入 diff；
- 生成文档无漂移；
- 已删除的旧迁移脚本没有被任何路径引用；
- 报告、API、Skill 和部署文档的语义一致。

提交 feature branch 后审阅：

```bash
git diff main...HEAD --stat
git diff main...HEAD --check
git log --oneline -1
```

确认无误后合并并推送：

```bash
git switch main
git pull --ff-only origin main
git merge --no-ff <feature-branch>
git push origin main
git rev-parse HEAD
```

记录最终 SHA；服务器只允许部署这个 SHA。

## 2. 服务器拉取代码

以下命令中的 `<server>`, `<repo>`, `<release_sha>` 和 `<deploy_user>` 由私有部署环境提供，不写入文档或日志。服务器上使用发布目录，不在运行目录直接 `git pull`：

```bash
ssh <deploy_user>@<server>
cd /opt/vitalis
git fetch --prune origin
git show --no-patch --format='%H %s' <release_sha>
git worktree add --detach /opt/vitalis/releases/<release_sha> <release_sha>
cd /opt/vitalis/releases/<release_sha>
uv venv --python 3.12 /opt/vitalis/venvs/<release_sha>
uv sync --python /opt/vitalis/venvs/<release_sha> --locked --extra dev
```

如果服务器采用源码镜像而不是 Git worktree，也必须把镜像固定为该 SHA，并在部署记录中保存 SHA、依赖锁文件 hash 和生成时间。

## 3. 停止服务和备份数据库

先确认当前服务和数据库路径：

```bash
systemctl status vitalis-api vitalis-worker --no-pager
systemctl cat vitalis-api vitalis-worker
grep -E '^(DATABASE_URL|VITALIS_ENV|ZEPP_MOCK|VITALIS_TIMEZONE)=' /etc/vitalis/vitalis.env
```

停止 API 和 worker，等待进程退出：

```bash
sudo systemctl stop vitalis-worker vitalis-api
pgrep -af 'vitalis (serve|worker)' || true
```

SQLite 数据库必须使用当前版本的备份命令或等价的 WAL-safe 流程，目标必须是新的带时间戳文件：

```bash
<release-venv>/bin/vitalis db backup \
  --output /var/backups/vitalis/vitalis-<old_sha>-<timestamp>.sqlite
sha256sum /var/backups/vitalis/vitalis-<old_sha>-<timestamp>.sqlite
```

PostgreSQL 环境必须使用 `pg_dump`/`pg_restore` 并保存 schema 和数据的校验记录。未知数据库后端不能假设 SQLite 命令适用。

## 4. 破坏性数据库更新

预发布不运行旧 schema 兼容迁移链。需要破坏性更新时：

1. 旧库停写并完成备份；
2. 用当前代码创建新的候选数据库和当前 schema；
3. 如果需要保留数据，执行一次明确、可审计的导出/导入，而不是运行长期兼容层；
4. 在候选库运行 `vitalis doctor`、schema 检查、合成报告、受影响测试和 API/worker smoke；
5. 验收通过后切换 `DATABASE_URL` 或原子替换数据库路径；
6. 重启服务并完成健康检查；
7. 观察期结束、备份校验通过并得到明确清理授权后，才删除旧数据库。

删除旧库前必须同时保留：

- 旧库原始备份；
- SHA256 或等价校验值；
- 新库 schema revision；
- 数据导入/丢弃清单；
- smoke test 输出；
- 部署 SHA、依赖版本和时间戳。

不要重新加入旧版数据库迁移脚本作为通用兼容方案。当前项目需要的是“备份旧数据、验证新库、切换、保留回滚备份”，不是持续维护历史 schema。

## 5. 切换 systemd 服务

当前 service unit 固定使用 `/opt/vitalis/.venv/bin/vitalis`。切换发布版本时，修改 `/etc/systemd/system/vitalis-api.service` 和 `/etc/systemd/system/vitalis-worker.service` 的 `ExecStart`，指向已验证的 release venv，确保 API 和 worker 相同：

```ini
ExecStart=/opt/vitalis/venvs/<release_sha>/bin/vitalis serve
ExecStart=/opt/vitalis/venvs/<release_sha>/bin/vitalis worker
```

修改 unit 后执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable vitalis-api vitalis-worker
sudo systemctl start vitalis-api vitalis-worker
sudo systemctl status vitalis-api vitalis-worker --no-pager
```

服务仍应使用 `/etc/vitalis/vitalis.env`，用户数据仍位于 `/var/lib/vitalis`，不把数据库或凭据复制到 release 目录。

## 6. 部署验收

```bash
curl --fail http://127.0.0.1:8000/live
curl --fail http://127.0.0.1:8000/ready
<release-venv>/bin/vitalis doctor
systemctl is-active vitalis-api vitalis-worker
journalctl -u vitalis-api -u vitalis-worker -n 100 --no-pager
```

继续验证：

- worker heartbeat 在数据库中更新；
- API 和 worker 使用同一 release SHA、数据库和时区；
- 合成数据能生成 morning/daily/evening/weekly/monthly 报告；
- PushPlus 测试配置为空，没有真实通知发送；
- `/api/data-status` 能显示最近成功同步和 signal coverage；
- 没有凭据、健康原文或用户个人信息进入日志。

## 7. 失败回滚

如果 `/live`、`/ready`、doctor、worker heartbeat、合成报告或数据库检查失败：

```bash
sudo systemctl stop vitalis-worker vitalis-api
# 将 unit 恢复到上一份已验证 release venv
sudo systemctl daemon-reload
# 如数据库已切换，使用保留的、校验过的旧库备份恢复到新的候选路径
sudo systemctl start vitalis-api vitalis-worker
```

回滚后重新检查健康探针和 worker；不要在异常状态下盲目重发 PushPlus。记录失败阶段、退出码、release SHA 和下一步，不记录 token、Cookie、数据库内容或健康原文。
