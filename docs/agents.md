# 智能体与客户端集成

[文档中心](README.md) | [快速开始](quickstart.md) | [报告与渠道](reports.md) | [产品 Skill](../skills/vitalis/SKILL.md)

本页区分仓库中的产品 Skill、HTTP API 和可选 Hermes 对话入口。Skill 是离仓可复制的薄客户端，不依赖仓库根目录；它不实现健康算法、不绕过鉴权，也不要求 PushPlus 报告回复。

## 鉴权和身份

用户范围请求必须带与本地用户绑定的 `Authorization: Bearer <token>`。令牌由 `vitalis token issue` 签发，scope 包括 `read`、`analyze`、`sync`、`feedback` 和 `manage`；只授予完成当前动作所需的最小 scope。`X-User-Id` 只能做一致性检查，不能代替 Bearer 认证。浏览器配对码和 browser-link token 不是 API 读取令牌。

非本机 Skill API 源必须使用 HTTPS；客户端拒绝含路径、凭据、查询或片段的 origin，并拒绝重定向。令牌优先从 `VITALIS_ACCESS_TOKEN_FILE` 指定的绝对路径私密文件读取，每次请求重新读取，支持原子替换后的热加载；未配置文件模式时才使用 `VITALIS_ACCESS_TOKEN` 私密环境。文件配置无效时直接失败，不回退到旧环境令牌。令牌不放命令行、URL、日志或对话。服务端错误会清洗供应商响应和数据库内容。API 使用真实的 4xx/5xx 状态码，错误正文统一返回 `state=failed`、`failure_code`、`message`、`retryable`、`next_action` 和 `request_id`。`next_action` 区分重新认证、补齐权限、修正请求、刷新状态后重试、稍后重试和检查服务；不能把失败正文当作成功结果或健康观测。

## Skill 的读写合同

将 `skills/vitalis/` 目录复制到任意位置，在私有环境设置 `VITALIS_API_BASE_URL` 和 `VITALIS_ACCESS_TOKEN`，运行：

```bash
python /path/to/vitalis/scripts/vitalis_api.py report daily --day 2026-10-07
```

固定读取命令包括 `status`、`report`、`workouts`、`job` 和白名单 `query`。`report daily --day YYYY-MM-DD --state` 读取报告新鲜度、`last_good_snapshot`、任务及下一步；更新期间可以呈现同日期上一份成功事实，并明确它正在更新。读取不会启动同步或分析；`report` 的 404 会转换为 `status=snapshot_missing`，不能改查其它日期或编造结果。完整操作表由 `tools/generate_api_reference.py` 从当前 OpenAPI 生成，见 [references/api.md](../skills/vitalis/references/api.md)。

`analyze`、`sync` 和 `feedback` 只有用户明确要求才调用。分析/同步写请求必须用持久 `--key-file`；同一个任务、参数和不确定响应复用同一键，客户端不会自动重试写请求。反馈 JSON 从标准输入传入，不能把备注放命令行；关联训练需要 `workout_id` 与 `workout_source`，RPE 还需要完成训练。资料 patch 使用 revision，冲突先重新读取。写入只改变明确的用户输入或反馈，不由发送、打开或沉默推断完成。

## 回答规则

客户端使用自然中文，先理解并回答当前问题，不把每个问题都改写成报告朗读。普通术语、训练原理、动作或营养知识问题可直接解释，无需查询个人 API；涉及用户当前状态、历史记录或“为什么适合我”时，读取最少相关事实，允许组合 `report`、`workouts` 和白名单 `query`，不固定成单一接口。保留 API 单位、来源、观测时间、日期口径和局部缺口；同一轮已经取得的相关事实可复用，不能把旧会话当作实时事实。

明确区分 Vitalis 已保存事实与结论、用户本次自述、一般性教练知识和未知项。允许解释、总结已有建议的取舍、讨论一般选项及明确标为假设的示例，但不重新计算个人趋势、相关、恢复分数、热量目标或另造个体训练处方；不把教练知识说成 Vitalis 的计算结果，不覆盖已有建议。缺失不是零，未知训练日不是休息日，厂商 readiness/Charge 和 `shadow_only` 不当作正式判断；不得由短窗口拼成周/月结果或诊断疾病。

只在会改变安全性或答案方向时问 1～2 个关键问题，不反复询问已经知道的内容，也不把简单问题变成长问卷。当前红旗症状先做安全分流，不等工具返回再提醒。回答可简短，也可按用户要求深入解释，不强迫每次套用固定结构。Hermes 只有在用户明确授权时才能写反馈、资料、推荐完成或事件确认；讨论、选择建议和分享感受不等于同意记录。正式报告生成仍使用确定性引擎和已保存事实，不自由生成健康百分比、诊断或推送版式。

### 教练知识 Skill

[healthy-fitness-coach 适配包](../skills/healthy-fitness-coach/SKILL.md)来自用户指定的 [Surge-Dan/healthy-fitness-coach-skill](https://github.com/Surge-Dan/healthy-fitness-coach-skill)，固定上游提交 `d4471a86b2edc8dedd2d86ad115476f9edd43a5a`。这是经过取舍的知识型适配，不是完整原版：只带安全、证据、训练原则、动作、营养与恢复 Markdown，保留 MIT 许可和逐文件来源；不安装上游脚本、评分/准备度引擎、训记 MCP、图片或自动档案/训练 DNA 功能。来源作者的研究审核日期与观点不代表本项目已独立完成医学事实核查。

部署时将两个 Skill 目录复制到私密 `HERMES_HOME/skills/`，并采用[人设模板](../deploy/hermes/SOUL.md)。普通教练知识由 `healthy-fitness-coach` 解释，用户个人事实由 `vitalis` 读取，两者不能建立两套档案或计算系统。Skill 参考资料不提供额外工具授权；不因为其中的描述安装依赖、访问凭据、联网连接训记或默认输出私人记录文件。既有模型服务仍会处理对话与必要 API 上下文，不能声称所有数据完全离线。新增的外部服务、长期个人记忆或导出文件需要另行明确授权；当前关闭 Hermes 的自动 memory 和 user profile。

验收至少覆盖：术语问题不查个人数据；个人问题组合必要只读查询；缺失数据不造分数；讨论感觉不落库；明确写入走现有幂等 API；红旗症状先分流；参考资料不能扩大权限。静态合同测试不能替代实际 agent 工具调用验收。

## 本地 Hermes 与 Telegram

链路是 Telegram → Hermes 自带网关 → `vitalis` Skill → 本机 Vitalis API；异步写入由 Vitalis worker 消费。不需要另建 Telegram 服务，也不需要把本地 API 暴露到公网。Hermes 的模型服务需要独立的有效登录或 API 凭据，Vitalis 本身不使用模型计算健康事实。

为此次联调建立独立的私密 `HERMES_HOME`，放在仓库忽略的 `.tmp/hermes-telegram-local/home/`；合成数据库、令牌、日志和工作目录放在同级目录。只复制需要的 Hermes 模型配置和机器人配置，不混入既有会话或真实健康数据库。这个目录是持续使用的本地实例，停止联调后再清理；一次性测试目录仍按仓库规则删除。不要把它提交或打进发布包。

1. 按[快速开始](quickstart.md)生成新的合成库，显式设置 `ZEPP_MOCK=true`、`VITALIS_ENV=test` 和空的 PushPlus 配置。API、worker、签发令牌的进程使用同一个 `DATABASE_URL` 和 `VITALIS_TIMEZONE`。本地端口可使用 `18000`，只监听 `127.0.0.1`。
2. 将整个 `skills/vitalis/` 复制到 `<HERMES_HOME>/skills/vitalis/`。在独立的 `config.yaml` 中设置 `terminal.backend: local`、`terminal.cwd` 为隔离工作目录，给 CLI 和 Telegram 启用 `terminal`、`skills` 工具。使用已安装 Hermes 支持的模型和有效认证，先验证模型调用成功。
3. 在该 home 的私密 `.env` 设置 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_ALLOWED_USERS`（本人数字 ID）和 `TELEGRAM_HOME_CHANNEL`（本人私聊 ID），并设置 `TELEGRAM_ALLOW_ALL_USERS=false`。令牌不放命令行或聊天。`SOUL.md` 要求先读取 Vitalis Skill，再调用客户端；验收期间每次健康回答明确标注“合成演示数据”。不要把仓库开发规则当作健康助理的人设。
4. 为合成用户签发 `read`、`analyze`、`sync`、`feedback` scope 的令牌；需要验证资料、偏好或目标修改时才增加 `manage`。令牌文件放在同级私密目录，使用 `token issue --output` 写入新路径。启动独立 API 和 worker，检查 `/live`、`/ready` 和 `doctor` 的 worker 心跳。

使用[启动器](../tools/run_hermes.py)验证 Vitalis 令牌文件，把绝对文件路径注入 Hermes 子进程的 `VITALIS_ACCESS_TOKEN_FILE`，清除继承的 `VITALIS_ACCESS_TOKEN`；Skill 每次请求读取文件中的最新令牌，轮换后无需重启网关。它校验 API origin 与安装目录，使用独立 home 作为工作目录，并把临时文件固定到仓库 `.tmp/hermes-launcher/`；令牌不进入进程参数。旧的 `VITALIS_API`、`VITALIS_USER` 不能替代当前 Bearer 配置。命令默认从仓库根目录执行：

```powershell
uv run --locked --extra dev python tools/run_hermes.py --home .tmp/hermes-telegram-local/home --token-file .tmp/hermes-telegram-local/vitalis.token --api-base-url http://127.0.0.1:18000 -- -s vitalis -z "读取今天的日报，注明合成演示数据，不写反馈。"
uv run --locked --extra dev python tools/run_hermes.py --home .tmp/hermes-telegram-local/home --token-file .tmp/hermes-telegram-local/vitalis.token --api-base-url http://127.0.0.1:18000 -- gateway run
```

`--hermes-command` 可指定已安装的 Hermes 可执行文件。启动器保留 Hermes 的退出码；模型未登录、服务不可达和工具失败都不算联调成功。同一机器人只能有一个 polling 网关：切换前停止旧 home 的网关，停止时也必须指定正确的 `HERMES_HOME`。Windows 后台运行用隐藏窗口；前台运行可用 Ctrl+C 停止。API、worker 与网关的进程要分别管理，关闭聊天窗口不等于它们停止。

使用第三方兼容模型服务时，在 Hermes 的 `model` 配置中设置 `provider: custom`、`base_url`、`default`（服务实际提供的模型名）和 `key_env: OPENAI_API_KEY`，并按服务实际协议设置 `api_mode`。模型地址包含其要求的 API 前缀（例如 `/v1`）；启动器的 `--api-base-url` 指向 Vitalis API。将模型密钥原文作为单独一行保存在私密文件中，在上述启动命令的 `--` 之前增加 `--model-key-file .tmp/hermes-telegram-local/model-api.key`。启动器仅向子进程的 `OPENAI_API_KEY` 注入模型密钥；不会把它写进 YAML、命令参数或日志。未提供这个参数时保留 Hermes 已有的认证方式。

Windows 上若 Git 装在非默认目录，在该 home 的私密 `.env` 中设置 `HERMES_GIT_BASH_PATH` 为 Git Bash 可执行文件的绝对路径。系统的 `bash.exe` 可能是 WSL 入口，不能处理 Hermes 为本机后端生成的 `/d/...` 路径；先通过 Hermes 终端工具读取一次 Skill `status`，确认返回绑定用户且退出码为零。

Windows 系统代理未必会被 Hermes 的 Telegram transport 自动采用。若系统网络请求成功但网关连接 Telegram 超时，在该 home 的私密 `.env` 中显式配置 `TELEGRAM_PROXY`，然后正常停止并重新启动网关。已有 Windows 开机计划任务时，`gateway stop` 只停止当前进程；迁移到独立 home 或服务器前还要禁用旧自启动项。若任务管理返回“拒绝访问”，由用户在管理员 PowerShell 执行 `Disable-ScheduledTask -TaskName '<旧任务名>'`，再核对任务 `Enabled=false`，避免下次登录时旧网关重新抢占同一机器人。

在 Telegram 私聊里验证：读取指定日期报告；询问尚无快照的日期；只讨论建议但不要求记录；明确要求记录一条合成反馈并查询写入结果；明确要求分析并等待任务完成；重启网关后再次读取。只讨论建议不能产生反馈；写入后先核对 API 成功结果再回复“已记录”。检查白名单拒绝陌生账号的路径时使用合成测试，不向他人发测试消息。`getMe` 成功只证明 bot token 有效；必须收到用户发起的消息、执行 Skill 并把结果返回该私聊，才能确认 Telegram 对话验收。

## 移到服务器

服务器继续使用 Hermes 自带网关。按[部署 SOP](deployment.md)传输已验证的候选提交，安装匹配版本的 Hermes，将 Skill 复制到服务器私密 home；重新签发绑定真实用户的最小 scope Vitalis 令牌，配置服务器自己的模型认证、机器人白名单和 `SOUL.md`。不要上传本地合成数据库、演示令牌、会话日志或带演示日期的人设。

Hermes 与 Vitalis API 位于同一主机时，仍使用 `http://127.0.0.1:<port>`；跨主机必须 HTTPS，不能为迁就容器、WSL 或私有网段而放开 Skill 的明文远程 origin 限制。服务的 `ExecStart` 使用服务器锁定环境中的 Python、候选提交的 `tools/run_hermes.py` 和私密文件路径，`WorkingDirectory` 使用该 release。先停止本地同一机器人的网关再启动服务器网关；不要让双方争抢 Telegram 更新。最后从本人私聊重新完成读取、明确授权写入和重启验收。

Linux 的 systemd 模板见 [vitalis-hermes.service](../deploy/systemd/vitalis-hermes.service)。将模板的路径替换为实际固定的 Vitalis release/venv 和 Hermes release/venv；Hermes 使用与本地验收一致的 `0.21.3`、源码提交 `1ec61e4df2f08d2af0de059daf7ec16814daff28`，以自身 `uv.lock` 安装 `messaging` extra，依赖环境与 Vitalis 分开。服务以独立 `vitalis-hermes` 用户运行，只通过有范围的 Bearer API 访问健康数据，不授予真实数据库目录权限。

模板使用启动器的 `--exec`，在 POSIX 上让 Hermes 替换启动器进程，使 systemd 的 `MAINPID`、停止标记和重启状态码对应实际网关；Windows 使用默认启动方式。预先建立服务可写的 `<Vitalis release>/.tmp/hermes-launcher`，并核对 `ProtectHome=true` 下两个虚拟环境的底层 Python 位于 `/opt` 或系统目录。服务使用服务器常驻网络代理；不能依赖电脑的 SSH 反向转发。切换前保留旧 unit 的私密回滚副本；验收后按部署 SOP 清理，既有 Hermes 私密 home 和历史会话不当作测试目录覆盖或删除。

## Hermes 令牌自动轮换

同机部署使用独立的 [renewal service](../deploy/systemd/vitalis-token-renewal.service) 和 [timer](../deploy/systemd/vitalis-token-renewal.timer)，不由模型或 Skill 签发。任务以 `vitalis` 账号访问数据库，Hermes 仍只有 scoped API 权限。将模板路径固定到验收 release/venv；root 管理的 `/etc/vitalis/token-renewal.env` 仅设置 `VITALIS_ROTATION_USER=<已核实的用户>`，scope 在 unit 中明确列出并须与现有令牌完全一致。

- 预先建立 `/var/lib/vitalis-hermes-credentials`，属主 `vitalis:vitalis-hermes`、权限 `0750`；令牌文件同属主、权限 `0640`。Hermes 只能读取，不能替换文件或修改目录；不把 Hermes 加入数据库服务组。首次凭据由管理员用 `token issue --output` 签发，轮换不以缺失文件为由自动创建身份。
- 模板保持新令牌有效期 90 天、剩余 14 天时轮换、旧令牌最长 300 秒交接期；未到期窗口不重复签发。固定到期不等于人工操作周期。启用 timer 后每小时自动检查（最多随机延迟 5 分钟，关机错过后补检查）；首次配置后手动启动 service，核对 `Result=success` 与安全输出 `state=not_due` 或 `state=rotated`。
- 流程为锁定凭据 → 核对用户/scope/撤销状态 → 提交新令牌 → 通过本机 API 验证身份 → 原子替换凭据 → 缩短旧令牌到期时间。探测绕过代理、不跟随重定向；新旧令牌不进入命令参数或日志。可用同参数 `token rotate --force` 做受控轮换验收，但不在定时任务中使用 `--force`。
- Skill 每次请求读最新文件；部署新客户端时网关需要重启一次，此后的令牌轮换不需要重启。已经发出的请求可在交接期内继续使用旧凭据，写请求不因换令牌自动重试。
- 未知、已撤销或用户/scope 不匹配的凭据拒绝轮换；已到期但仍存在且授权匹配的凭据可由本机签发任务恢复。文件缺失、权限不安全、API 不通或数据库失败时保留原文件并报错，不能用永久令牌绕过。
- 数据库提交与文件替换不是跨资源事务：发布前失败会尽力撤销候选；发布后缩短旧令牌失败则保留已可用的新文件，并明确返回失败要求运维检查。极端进程崩溃可能留下两枚凭据直到各自原始有效期结束，不声称旧令牌一定已经撤销。需要撤销集成时先停 timer/service，再撤销当前文件对应令牌，避免与正在执行的本机签发竞争。

用 `systemctl status vitalis-token-renewal.timer vitalis-token-renewal.service` 和 `journalctl -u vitalis-token-renewal.service` 排查。失败输出只包含安全状态/原因，不输出凭据或健康响应；当前只记录本机日志和 failed unit 状态，不额外发送通知。每次发布时 renewal unit 与 API、worker、alerts、Hermes 一起更新到同一候选 SHA。

## 验收边界

离仓 Skill 的 mock 和合同测试只证明客户端边界、Bearer 处理和 JSON 合同；不证明真实 Hermes 环境、真实 Zepp 账户、浏览器扩展或 PushPlus 已联调。实际接入要在新临时目录使用合成数据，保存任务状态但不发送真实通知。遇到 401/403、404、网络错误、未完成任务和 `uncertain` 投递分别说明，不把其中任一项写成无数据或成功。

2026-10-28 本地已用独立合成库完成实际验收：第三方兼容模型 `gpt-6.1-sol` 的工具调用、Hermes 通过离仓 Skill 读取报告、只读请求不产生反馈或分析任务、明确授权的反馈仅提交一次且对应分析成功；本人从 Telegram 私聊发起测试，后台核对了入站消息、Skill 调用成功和带测试标记的合成回复，用户确认收到。旧网关已正常停止，旧 Windows 自启动任务已核对为禁用。当前本地实例绑定 `demo`；服务器切换真实用户时须重新签发令牌并完成同一收发验收。
