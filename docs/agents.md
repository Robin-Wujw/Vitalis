# 智能体与客户端集成

[文档中心](README.md) | [快速开始](quickstart.md) | [报告与渠道](reports.md) | [产品 Skill](../skills/vitalis/SKILL.md)

本页区分仓库中的产品 Skill、HTTP API 和可选 Hermes 对话入口。Skill 是离仓可复制的薄客户端，不依赖仓库根目录；它不实现健康算法、不绕过鉴权，也不要求 PushPlus 报告回复。

## 鉴权和身份

用户范围请求必须带与本地用户绑定的 `Authorization: Bearer <token>`。令牌由 `vitalis token issue` 签发，scope 包括 `read`、`analyze`、`sync`、`feedback` 和 `manage`；只授予完成当前动作所需的最小 scope。`X-User-Id` 只能做一致性检查，不能代替 Bearer 认证。浏览器配对码和 browser-link token 不是 API 读取令牌。

非本机 Skill API 源必须使用 HTTPS；客户端拒绝含路径、凭据、查询或片段的 origin，并拒绝重定向。令牌从 `VITALIS_ACCESS_TOKEN` 私密环境读取，不放命令行、URL、日志或对话。服务端错误会清洗供应商响应和数据库内容。API 使用真实的 4xx/5xx 状态码，错误正文统一返回 `state=failed`、`failure_code`、`message`、`retryable`、`next_action` 和 `request_id`。`next_action` 区分重新认证、补齐权限、修正请求、刷新状态后重试、稍后重试和检查服务；不能把失败正文当作成功结果或健康观测。

## Skill 的读写合同

将 `skills/vitalis/` 目录复制到任意位置，在私有环境设置 `VITALIS_API_BASE_URL` 和 `VITALIS_ACCESS_TOKEN`，运行：

```bash
python /path/to/vitalis/scripts/vitalis_api.py report daily --day 2026-10-07
```

固定读取命令包括 `status`、`report`、`workouts`、`job` 和白名单 `query`。`report daily --day YYYY-MM-DD --state` 读取报告新鲜度、`last_good_snapshot`、任务及下一步；更新期间可以呈现同日期上一份成功事实，并明确它正在更新。读取不会启动同步或分析；`report` 的 404 会转换为 `status=snapshot_missing`，不能改查其它日期或编造结果。完整操作表由 `tools/generate_api_reference.py` 从当前 OpenAPI 生成，见 [references/api.md](../skills/vitalis/references/api.md)。

`analyze`、`sync` 和 `feedback` 只有用户明确要求才调用。分析/同步写请求必须用持久 `--key-file`；同一个任务、参数和不确定响应复用同一键，客户端不会自动重试写请求。反馈 JSON 从标准输入传入，不能把备注放命令行；关联训练需要 `workout_id` 与 `workout_source`，RPE 还需要完成训练。资料 patch 使用 revision，冲突先重新读取。写入只改变明确的用户输入或反馈，不由发送、打开或沉默推断完成。

## 回答规则

客户端回答使用中文，优先呈现 API 返回的 `*_label`、报告 `sections`、单位、来源、观测时间和局部缺口。缺失不是零，未知训练日不是休息日，厂商 readiness/Charge 不当作 Vitalis 判定。不得自行计算趋势、相关、恢复或训练处方；不得由短窗口拼成周/月结果；不得诊断疾病。`open_health_insights` 只能按 `shadow_only` 描述。

正常报告先回答当前问题，不泄漏 PushPlus 配置或令牌。Hermes 只有在用户明确授权时才能写反馈、资料、推荐完成或事件确认；没有对话要求时不主动追问。报告生成始终使用确定性引擎和已保存事实，不自由生成百分比、诊断、HTML、链接、按钮或 CSS。

## 本地 Hermes 与 Telegram

链路是 Telegram → Hermes 自带网关 → `vitalis` Skill → 本机 Vitalis API；异步写入由 Vitalis worker 消费。不需要另建 Telegram 服务，也不需要把本地 API 暴露到公网。Hermes 的模型服务需要独立的有效登录或 API 凭据，Vitalis 本身不使用模型计算健康事实。

为此次联调建立独立的私密 `HERMES_HOME`，放在仓库忽略的 `.tmp/hermes-telegram-local/home/`；合成数据库、令牌、日志和工作目录放在同级目录。只复制需要的 Hermes 模型配置和机器人配置，不混入既有会话或真实健康数据库。这个目录是持续使用的本地实例，停止联调后再清理；一次性测试目录仍按仓库规则删除。不要把它提交或打进发布包。

1. 按[快速开始](quickstart.md)生成新的合成库，显式设置 `ZEPP_MOCK=true`、`VITALIS_ENV=test` 和空的 PushPlus 配置。API、worker、签发令牌的进程使用同一个 `DATABASE_URL` 和 `VITALIS_TIMEZONE`。本地端口可使用 `18000`，只监听 `127.0.0.1`。
2. 将整个 `skills/vitalis/` 复制到 `<HERMES_HOME>/skills/vitalis/`。在独立的 `config.yaml` 中设置 `terminal.backend: local`、`terminal.cwd` 为隔离工作目录，给 CLI 和 Telegram 启用 `terminal`、`skills` 工具。使用已安装 Hermes 支持的模型和有效认证，先验证模型调用成功。
3. 在该 home 的私密 `.env` 设置 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_ALLOWED_USERS`（本人数字 ID）和 `TELEGRAM_HOME_CHANNEL`（本人私聊 ID），并设置 `TELEGRAM_ALLOW_ALL_USERS=false`。令牌不放命令行或聊天。`SOUL.md` 要求先读取 Vitalis Skill，再调用客户端；验收期间每次健康回答明确标注“合成演示数据”。不要把仓库开发规则当作健康助理的人设。
4. 为合成用户签发 `read`、`analyze`、`sync`、`feedback` scope 的令牌；需要验证资料、偏好或目标修改时才增加 `manage`。令牌文件放在同级私密目录，使用 `token issue --output` 写入新路径。启动独立 API 和 worker，检查 `/live`、`/ready` 和 `doctor` 的 worker 心跳。

使用[启动器](../tools/run_hermes.py)从文件读取 Vitalis 令牌，注入 Hermes 子进程环境。它校验 API origin 与安装目录，使用独立 home 作为工作目录，并把临时文件固定到仓库 `.tmp/hermes-launcher/`；令牌不进入进程参数。旧的 `VITALIS_API`、`VITALIS_USER` 不能替代当前 Bearer 配置。命令默认从仓库根目录执行：

```powershell
uv run --locked --extra dev python tools/run_hermes.py --home .tmp/hermes-telegram-local/home --token-file .tmp/hermes-telegram-local/vitalis.token --api-base-url http://127.0.0.1:18000 -- -s vitalis -z "读取今天的日报，注明合成演示数据，不写反馈。"
uv run --locked --extra dev python tools/run_hermes.py --home .tmp/hermes-telegram-local/home --token-file .tmp/hermes-telegram-local/vitalis.token --api-base-url http://127.0.0.1:18000 -- gateway run
```

`--hermes-command` 可指定已安装的 Hermes 可执行文件。启动器保留 Hermes 的退出码；模型未登录、服务不可达和工具失败都不算联调成功。同一机器人只能有一个 polling 网关：切换前停止旧 home 的网关，停止时也必须指定正确的 `HERMES_HOME`。Windows 后台运行用隐藏窗口；前台运行可用 Ctrl+C 停止。API、worker 与网关的进程要分别管理，关闭聊天窗口不等于它们停止。

Windows 上若 Git 装在非默认目录，在该 home 的私密 `.env` 中设置 `HERMES_GIT_BASH_PATH` 为 Git Bash 可执行文件的绝对路径。系统的 `bash.exe` 可能是 WSL 入口，不能处理 Hermes 为本机后端生成的 `/d/...` 路径；先通过 Hermes 终端工具读取一次 Skill `status`，确认返回绑定用户且退出码为零。

在 Telegram 私聊里验证：读取指定日期报告；询问尚无快照的日期；只讨论建议但不要求记录；明确要求记录一条合成反馈并查询写入结果；明确要求分析并等待任务完成；重启网关后再次读取。只讨论建议不能产生反馈；写入后先核对 API 成功结果再回复“已记录”。检查白名单拒绝陌生账号的路径时使用合成测试，不向他人发测试消息。`getMe` 成功只证明 bot token 有效；必须收到用户发起的消息、执行 Skill 并把结果返回该私聊，才能确认 Telegram 对话验收。

## 移到服务器

服务器继续使用 Hermes 自带网关。按[部署 SOP](deployment.md)传输已验证的候选提交，安装匹配版本的 Hermes，将 Skill 复制到服务器私密 home；重新签发绑定真实用户的最小 scope Vitalis 令牌，配置服务器自己的模型认证、机器人白名单和 `SOUL.md`。不要上传本地合成数据库、演示令牌、会话日志或带演示日期的人设。

Hermes 与 Vitalis API 位于同一主机时，仍使用 `http://127.0.0.1:<port>`；跨主机必须 HTTPS，不能为迁就容器、WSL 或私有网段而放开 Skill 的明文远程 origin 限制。服务的 `ExecStart` 使用服务器锁定环境中的 Python、候选提交的 `tools/run_hermes.py` 和私密文件路径，`WorkingDirectory` 使用该 release。先停止本地同一机器人的网关再启动服务器网关；不要让双方争抢 Telegram 更新。最后从本人私聊重新完成读取、明确授权写入和重启验收。

## 验收边界

离仓 Skill 的 mock 和合同测试只证明客户端边界、Bearer 处理和 JSON 合同；不证明真实 Hermes 环境、真实 Zepp 账户、浏览器扩展或 PushPlus 已联调。实际接入要在新临时目录使用合成数据，保存任务状态但不发送真实通知。遇到 401/403、404、网络错误、未完成任务和 `uncertain` 投递分别说明，不把其中任一项写成无数据或成功。
