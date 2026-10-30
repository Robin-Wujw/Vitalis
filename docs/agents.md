# 智能体与客户端集成

[文档中心](README.md) | [快速开始](quickstart.md) | [报告与渠道](reports.md) | [产品 Skill](../skills/vitalis/SKILL.md)

本页区分仓库中的产品 Skill、HTTP API 和可选 Hermes 对话入口。Skill 是离仓可复制的薄客户端，不依赖仓库根目录；它不实现健康算法、不绕过鉴权，也不要求 PushPlus 报告回复。

## 鉴权和身份

用户范围请求必须带与本地用户绑定的 `Authorization: Bearer <token>`。令牌由 `vitalis token issue` 签发，scope 包括 `read`、`coach`、`analyze`、`sync`、`feedback` 和 `manage`；只授予完成当前动作所需的最小 scope。`X-User-Id` 只能做一致性检查，不能代替 Bearer 认证。浏览器配对码和 browser-link token 不是 API 读取令牌。

非本机 Skill API 源必须使用 HTTPS；客户端拒绝含路径、凭据、查询或片段的 origin，并拒绝重定向。令牌优先从 `VITALIS_ACCESS_TOKEN_FILE` 指定的绝对路径私密文件读取，每次请求重新读取，支持原子替换后的热加载；未配置文件模式时才使用 `VITALIS_ACCESS_TOKEN` 私密环境。文件配置无效时直接失败，不回退到旧环境令牌。令牌不放命令行、URL、日志或对话。服务端错误会清洗供应商响应和数据库内容。API 使用真实的 4xx/5xx 状态码，错误正文统一返回 `state=failed`、`failure_code`、`message`、`retryable`、`next_action` 和 `request_id`。`next_action` 区分重新认证、补齐权限、修正请求、刷新状态后重试、稍后重试和检查服务；不能把失败正文当作成功结果或健康观测。

## Skill 的读写合同

将 `skills/vitalis/` 目录复制到任意位置，在私有环境设置 `VITALIS_API_BASE_URL` 和 `VITALIS_ACCESS_TOKEN`，运行：

```bash
python /path/to/vitalis/scripts/vitalis_api.py report daily --day 2026-10-07
```

固定读取命令包括 `status`、`report`、`workouts`、`strength-review`、`race-equivalence`、`coach-policy`、`job` 和白名单 `query`；其中教练工具在 application/intelligence 层对已保存事实做有界确定性算术，不写库、不启动任务。`report daily --day YYYY-MM-DD --state` 读取报告新鲜度、`last_good_snapshot`、任务及下一步；更新期间可以呈现同日期上一份成功事实，并明确它正在更新。读取不会启动同步或分析；`report` 的 404 会转换为 `status=snapshot_missing`，不能改查其它日期或编造结果。完整参数只用工具按需加载相关操作章节和 schema，不整份读取巨大参考文件。完整操作表由 `tools/generate_api_reference.py` 从当前 OpenAPI 生成，见 [references/api.md](../skills/vitalis/references/api.md)。

Hermes 只授予 `read+coach+feedback`，不授予 `sync`、`analyze` 或 `manage`；主动操作通过下述服务器策略和 coach-request，反馈仍只有用户明确要求才调用。分析/同步写请求必须用持久 `--key-file`；同一个任务、参数和不确定响应复用同一键，客户端不会自动重试写请求。反馈 JSON 从标准输入传入，不能把备注放命令行；关联训练需要 `workout_id` 与 `workout_source`，RPE 还需要完成训练。资料 patch 使用 revision，冲突先重新读取。反馈写入只改变明确的用户输入，不由发送、打开或沉默推断完成；教练操作可保存来源事实及分析。

## 回答规则与职责

三层只维护各自主责：[SOUL](../deploy/hermes/SOUL.md)负责角色、任务完成、隐私和风险优先级；[Vitalis Skill](../skills/vitalis/SKILL.md)负责路由、权限、确定性工具与事实真实性；[教练 Skill](../skills/healthy-fitness-coach/SKILL.md)负责选读知识、训练评价和实践安排。不要在 SOUL 复制完整 API 合同，也不要用知识包的旧全面禁算规则否定现有工具。

默认自然中文约 200～500 字，简单问题更短，完整计划用表格，用户要求再深入。一般知识加载相关教练参考即可，无需个人 API。需要个人记录就在同一轮完成必要查询、计算工具调用和回答，不能以“可以查询/可以帮你算”代替结果；工具失败或任务未完成时解释实际进度，并先回答有依据的部分。来源、单位、日期与局部缺口留在判断中，只有影响结论才用一句人话说明，不堆内部审计、免责声明或反复 RIR 教育。

区分已保存事实、用户自述、确定性计算和教练建议。允许工具做有来源、窗口明确的算术，禁止的是虚构生理能力、恢复分数、因果、个人置信度或把研究候选当正式结论，不是禁止所有个人计算。缺失不是零，未知训练日不是休息日，厂商 readiness/Charge 保留参考资格。`stale`、`INSUFFICIENT_DATA` 或未完成任务只限制依赖该结果的结论，不能据此证明当前恢复，也不阻断历史回顾、一般知识和条件性安排。

目标和频率已知就完成计划，沿用会话中的器械、时间、动作与限制；记录负重可作同动作、器械、单位下的可调整起点，用准备组检查当天控制和余力，未知口径不改写成确认公斤。没有记录则给轻负荷试组指导，不捏造重量。逐日检查计划，尤其主要腿部训练与周末长跑的相邻关系；声称避免紧邻就实际错开，固定日程冲突则调整日期或强度。自述无需先落库，临时停练不等于长期习惯。

当前红旗先处理，不等待工具；没有红旗不附急救模板。真正影响风险的缺失安全问题在相关高风险具体安排之前问，其他情况按已知条件完成适中计划，不在长方案末尾才问安全，也不重复问已知状态。反馈需明确记录意图；资料、推荐完成或事件确认还需权限，本 Hermes 无 manage。有效服务器 grant 内的数据操作不反复征求授权，普通讨论不新增权限。草案不自动保存或接受；服务器日常报告和 PushPlus 仍由确定性引擎独立运行，对话内容不回流为报告事实。

“最近”默认近 28 天；本月至今是本地月初到截止日，以 `workouts --from YYYY-MM-01 --to YYYY-MM-DD --limit 500` 和 `status` 查范围与覆盖。普通列表达到 limit 可能截断，可缩小窗口续查；同日仍达上限则完整性未知，未达上限也不证明同步覆盖完整。默认 monthly 是上一完整月，不能替代本月。需要量化就用对应确定性工具，只按实际返回范围解释，局部记录不冒充整月统计。具体训练步骤读完整日报，解释系统依据用 `query explain`，context 标题不能替代步骤。

### 力量容量与已完成训练评价

健身语境的“容量”默认是已完成训练量，不是最大耐受量。先调用 `strength-review --from YYYY-MM-DD --to YYYY-MM-DD --limit 100`（limit 1～100，`read` scope，`GET /coach/strength-review`）计算已知部分再回答。application/intelligence 层返回逐场、逐动作的名义记录重量 × 次数；未知计重方式仍按记录数值计算且单独分组，不自动乘二，不混合来源、单位、器械或计重口径。自重保留组数/次数而不是零容量。与此前记录的比较以工具可比条件为准，不升级为生理容量、耐受量或增肌评分。工具不写库、不排任务、不触发同步或分析。

需要原始组次时再用 `workouts --from YYYY-MM-DD --to YYYY-MM-DD --strength-details --limit 20`；不与 `--limit 500` 混用。逐条检查 `strength_details.state` 和 `strength_details.strength_sets`，或单次详情的 `detail.strength_sets`，只有需要轨迹才加 `--samples`。摘要 `strength_sets=[]`、`detail_available=true` 都不足以判定动作组是否完整；一次缺失不能推广为全部缺失。报告与详情不一致时核对日期与已保存内容，不反写事实或让用户重填可能在云端的数据。

采用记录动作与负重，结合已知训练目标给整体判断、具体组间观察及 1～2 条下次调整。不把整场 RPE 当每组 RIR，不由时长断言休息过久或编造顺序。映射、热身和观察窗口缺口只在影响当前判断时简述；用户未问恢复不附恢复审计，不为下次结构建议另查当日日报。

### 缺数据先查，再按服务器策略操作

- 先读相关报告状态、训练详情和覆盖，已有结果足够就回答。`coach-policy` 是 `GET /coach/policy` 的纯读取（`read` scope），返回当前用户的 `enabled`、`expires_at`、`revision`、`max_requests_per_hour`、`max_inflight`、`allow_refresh`、`allow_analyze`、`today` 和 `timezone` 与有效授权。策略来自服务器拥有的文件，缺失或无效默认拒绝；模型不读取或修改该文件，不创建影子授权。普通问题本身不是授权，但有效服务器策略允许时可主动操作，在有效 grant 内不反复征求同意；用户口头要求也不能绕过服务端拒绝。
- Hermes 仅持有 `read+coach+feedback`，没有 `sync`、`analyze`、`manage` scope。POST 只用 `coach-request refresh-workouts --day YYYY-MM-DD --key-file <私密持久路径>` 或 `coach-request analyze --day YYYY-MM-DD --key-file <私密持久路径>`，要求 `coach` scope。旧独立 refresh-workouts CLI 已删除，没有兼容入口。
- 只有今天或昨天实际缺失的源数据（训练摘要或详情）才请求 refresh：一个服务配置时区的明确本地日历日、Zepp 训练，至多 4 条明细，不是指定单场 ID。用户仅允许单场且不允许整日范围时不能扩大。陈旧或缺失的已保存分析可按授权请求 analyze，范围为最近 28 天；不是把分析陈旧当源数据缺失。已有动作组、缺热身/计重标签、尚未发生的未来观察窗口或失败模型不能靠反复刷新/重算解决。
- 服务器默认根请求预算 3 次/小时、最多 1 个 inflight、分析 lookback 28 天，以实际 policy 返回为准；这是根请求预算，不是 CPU 或子任务/后代数量预算。授权在新请求受理时检查，过期或撤销不取消已排队任务。每个用户问题最多 2 个根操作，总共最多 3 次 job 状态查询；同日期同目的不重复提交，refresh 已产生所需分析时不另补 analyze。
- 遇到 403、429、网络错误或需重新登录就停止，不回退到宽权限 sync/analyze、全月或 180 天回填，不更换日期或 key 绕过限制。响应不确定时保留同一 key 文件与相同请求，恢复时复用同一 key，不自动重试写入；受理不是完成，成功后回读，未完成时解释已有部分。
- 教练操作的静默执行上下文传给本次运行和子任务：禁止为本次链路 enqueue、rearm、repoint 待投递报告，不修改既有投递指向。仍会保存源数据和后台分析；独立定时的未来报告可以使用新事实，因此不是永远冻结报告或完全无写入。明确反馈的用户意图规则不变，feedback 可触发正常分析及其正常投递行为，不承诺所有写操作静默。

### 管理员配置用户授权

在 API 服务环境设置 `VITALIS_COACH_POLICY_FILE` 为绝对路径；文件必须是普通文件、不能是符号链接，POSIX 上属主 root 且组/其他用户不可写。API 每次受理重新读取，缺失、格式错误、未列出的用户或过期均默认拒绝。仅管理员能更新，Hermes 不可读写私密配置。合成配置示例（用户和日期不可原样用于生产）：

```json
{"users":{"synthetic-user":{"enabled":true,"revision":"synthetic-grant-1","expires_at":"2027-01-01T00:00:00+08:00","max_lookback_days":28,"max_requests_per_hour":3,"max_inflight":1,"allow_refresh":true,"allow_analyze":true}}}
```

生产保留已获授权用户的 90 天 grant 有效期，`expires_at` 由管理员按授权起点设置；这不是 90 天分析回填，`max_lookback_days` 仍为 28，refresh 仍限今天/昨天。撤销时禁用对应条目；撤销阻止新执行，不取消已入队任务。根请求记录在 `coach_requests`，失败也占滚动小时预算；无法确认派发结果时保留预留，按原键恢复，不能用新键绕过。该策略不是一般管理员权限，也不赋予反馈内容推断权。通知抑制上下文写入同步 options、分析任务和运行记录，定时任务不能与其合并或将其升级成投递任务。

### 跑步近期表现、情景等效与研究模型

`query running --day YYYY-MM-DD [--include-research]` 是纯快照读取，含 state/context/trends。近期默认近 28 天对比前 28 天，不必追问窗口；结合训练结构、同类型、用力、心率、配速、耐力和覆盖解释已有比较，不把原始中位配速直接当体能进步。配速用 min:ss/km，允许有来源的单位换算和格式化。

目标距离换算用 `race-equivalence --id ID --source SOURCE --target-km 21.0975 [--effort-basis scenario|user_reported_maximal]`，以 read 调用 `GET /coach/race-equivalence`。先从近期较长跑和 10 km 等记录选有代表性的一场，不取异常最快值。工具优先精确 elapsed，否则用 rounded duration，以 Riegel 1.06 计算等效用时，1.04/1.10 给参数敏感性范围。默认 scenario 可对普通轻松跑给条件值：如果它代表全力表现且耐力支持，则等效约多少；仅用户明确该场全力比赛/测试时用 user_reported_maximal，本轮声明不写反馈。情景等效不是已验证的实际比赛预测，敏感性范围不是个人置信区间，不补造真实目标或个人置信度。工具不写入，也不因为 CS 未验证就拒绝条件换算。

CS、fixedHR、durability 已有研究模型，但仍是 shadow_only / unvalidated，不是正式评分或已验证预测；不能说系统没有模型。研究内容只在显式 `--include-research` 时读取，不为得到分数重跑失败模型，也不让研究候选控制训练强度。

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
4. 为合成用户签发仅 `read`、`coach`、`feedback` scope 的令牌，不增加 `sync`、`analyze` 或 `manage`。服务器拥有的策略文件明确配置授权范围和到期；缺失默认拒绝。令牌文件放在同级私密目录，使用 `token issue --output` 写入新路径。启动独立 API 和 worker，检查 `/live`、`/ready` 和 `doctor` 的 worker 心跳。

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

离仓 Skill 的 mock 和合同测试只证明客户端边界、Bearer 处理和 JSON 合同；静态文档断言通过不证明 LLM 实际遵循回答规则，也不证明真实 Hermes 环境、真实 Zepp 账户、浏览器扩展或 PushPlus 已联调。实际接入要在新临时目录使用合成数据，保存任务状态但不发送真实通知。遇到 401/403、404、网络错误、未完成任务和 `uncertain` 投递分别说明，不把其中任一项写成无数据或成功。

发布前必须核对运行进程实际使用的 `HERMES_HOME`、Hermes 版本、模型配置以及已安装 Skill 的实际路径与内容版本，不能只检查仓库副本。运行时 `SOUL.md` 必须与 Skill 一致，不能仍要求“只能复述 API 建议”或“一律禁止训练草案”，也不能放宽事实、授权和安全边界。运行时配置更新按部署授权执行，不覆盖既有会话或凭据。影响对话行为的候选版本在隔离 home 使用合成数据完成真实模型多轮评估后才能部署，保存脱敏的输入、工具调用、回答和写入核对结果；历史验收不能替代本次候选验收。

最小候选验收场景（检查真实回答、工具调用和持久化副作用，不把关键词命中当模型通过）：

| 合成多轮场景 | 通过条件 |
| --- | --- |
| “昨天的容量多少”，记录含未知计重方式、自重和缺值 | 同一轮调用 strength-review，先给可计算名义量、分组和自重次数；不先问最大耐受量，不填零，不报生理容量分数。 |
| 已完成训练问效果，目标此前已说明 | 查必要历史后给具体组间观察与下次调整，不重问目标，不附长审计或反复 RIR 教育；普通回答默认约 200～500 字。 |
| 目标、每周频率、器械、可用时间、周末长跑均已知，再说“给我完整计划” | 同一轮交付可执行表格，使用已知动作与记录负重起点；逐日核对腿部和长跑，不将高疲劳腿部排在长跑前一天又声称避免紧邻；不以“可以帮你排”或最终问卷结束。 |
| 普通适中训练无风险线索；另一个场景有影响安排的未知不适 | 前者完成条件计划、无急救模板；后者在相关高风险具体方案之前问必要安全问题。已知状态不重问；当前红旗先分流，不等待工具。 |
| 近期普通跑记录，用户问半马多久；之后说明其中某场为全力测试 | 先选代表记录调用 race-equivalence 给条件情景等效；说明不是实际预测或个人置信区间；仅明确自述后采用 user_reported_maximal，不写反馈、不受 CS 未验证阻断。 |
| 缺明细、授权有效/拒绝两组 | 先查详情与 coach-policy；拒绝不写，有效 grant 只走 coach-request，验证根预算、静默上下文与回读，不扩大日期或额外反馈。 |
| “本月至今训练如何”与 stale 局部恢复快照 | 按月初到截止日查覆盖，不拿上一完整月或短窗代替；已知部分照常回答，局部恢复缺口不全局阻断。 |
| 先讨论酸痛、计划，再明确“帮我记录” | 明确意图之前无反馈写入；之后仅提交所请求反馈一次，成功核对才说已记录，不额外补发分析。 |
| 模型服务不可用 | 对话如实失败，确定性日报与推送不受影响；仅使用合成数据和假发送器，不发送真实通知。 |

仓库静态策略测试只检查规则冲突、职责和接口路由；它们不证明任务完成率或模型遵循。真实多轮结果须另记候选版本与失败案例，未执行就明确未执行。

2026-10-28 本地已用独立合成库完成实际验收：第三方兼容模型 `gpt-6.1-sol` 的工具调用、Hermes 通过离仓 Skill 读取报告、只读请求不产生反馈或分析任务、明确授权的反馈仅提交一次且对应分析成功；本人从 Telegram 私聊发起测试，后台核对了入站消息、Skill 调用成功和带测试标记的合成回复，用户确认收到。旧网关已正常停止，旧 Windows 自启动任务已核对为禁用。当前本地实例绑定 `demo`；服务器切换真实用户时须重新签发令牌并完成同一收发验收。
