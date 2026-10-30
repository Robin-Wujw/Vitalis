---
name: vitalis
description: Retrieve Vitalis facts, use bounded deterministic coaching tools, complete grounded answers, and record explicitly authorized feedback.
---

# Vitalis 数据、工具与权限

使用[独立 HTTP 客户端](scripts/vitalis_api.py)，只需 Python 3.11+ 标准库，不依赖仓库或工作目录。完整参数见[API 操作清单](references/api.md)，按需定位相关操作章节和 schema，不整份读取。教练知识与计划取舍由 `healthy-fitness-coach` 提供；本 Skill 负责取数、确定性工具、权限与结果真实性。

## 配置与事实

- 运行 `python <Skill目录>/scripts/vitalis_api.py <命令> ...`。`VITALIS_API_BASE_URL` 是无路径、凭据、查询或片段的 origin，非本机必须 HTTPS，不跟随重定向。令牌优先由 `VITALIS_ACCESS_TOKEN_FILE` 的绝对私密普通文件读取，每次请求重新加载；不接受符号链接，POSIX 不得他人可读或组可写。未配置文件才用 `VITALIS_ACCESS_TOKEN`；文件配置失败不回退。令牌不放命令行、日志或聊天，`X-User-Id` 不选择身份。
- 区分 API 事实、用户自述、工具计算和教练建议。保留来源、单位、计重方式、观测时间、日期口径、窗口、样本与覆盖；不同用户、设备、来源或单位不混成同一事实。缺失不是零，未知训练日不是休息日。`synthetic`/`mock` 保留测试标签，不把合成来源说成设备实测。资格信息用于判断，只有影响答案时用一句人话说明，不逐项输出内部审计。
- 确定性工具可以计算有来源的记录量、比较与条件情景，单位换算和格式化也允许。把工具返回的名义量与真实生理能力分开，不虚构恢复分数、趋势、因果、肌肉收益或个人置信度；研究或厂商 readiness/Charge 不升级为正式结论。教练判断不冒充后端已验证安排。
- 陈旧只限制依赖该快照的结论：注明相关日期后继续回答历史事实、一般知识和有依据的条件性建议，不用旧快照证明当前恢复。`INSUFFICIENT_DATA`、未完成任务和错误不是零或当前结论。一般知识无需个人 API；对话已经取得且仍适用的事实可以复用。
- 需要个人数据的任务在同一轮取数、调用工具并给出结果，不以“可以查询”代替执行。服务器推送继续使用确定性引擎、已保存报告和固定渠道排版，不依赖模型在线；聊天草案不改写快照、不自动进入推送，也不自动成为已接受计划。没有计划保存接口就不能声称已保存。

## 按问题读取

| 任务 | 最少相关读取 |
| --- | --- |
| 当前适合怎样练 | `report daily --view` 读已保存建议；必要时 `--state` 或 `status` 核对新鲜度和覆盖。 |
| 具体训练步骤、解释减量 | 按需读完整 `report daily --day YYYY-MM-DD`；`query explain --day YYYY-MM-DD` 读真实触发事实与 `evidence_refs`。`query context` 的标题不是完整步骤，助手草案不用伪造系统证据。 |
| 目标、偏好、近期安排 | 只补确实未知的 `query profile`、`query training-preferences`、`workouts`；已知目标、时间和限制不重复询问。 |
| 完成了多少力量训练、同动作比较 | 使用下节 `strength-review`；需要原始组次再读力量明细。 |
| 近期跑步、比赛距离等效 | 使用跑步节的快照与 `race-equivalence`，不要把情景当实际预测。 |
| 主观感觉与明确记录 | 讨论感受不写入；明确要求记录才用 `feedback`，不替用户猜评分。 |

`status` 查同步覆盖，`job ID` 查任务；`report` 支持 daily/morning/evening/weekly/monthly/weekly-briefing/monthly-briefing。`--view` 是公开 blocks，`--state` 是新鲜度、last-good、任务与下一步，两者互斥。`query` 只用客户端白名单，按需组合 `trends`、`training-responses` 等相关读取。查询不会启动同步或分析。`status=error`、401/403、网络故障和 `status=snapshot_missing` 分别处理；后者仅表示指定日期无报告，不能自动改日期或编造结果。

默认“最近”取近 28 天；跑步比较前 28 天。日期按用户本地语境，部署默认 `Asia/Shanghai`，API 或用户给出不同口径时保留差异，只有实质歧义才澄清。本月至今取本地月初至截止日：`workouts --from YYYY-MM-01 --to YYYY-MM-DD --limit 500`，配合 `status`。普通列表达到 limit 可能截断，可缩小窗口续查，同一天仍达到上限就说明完整性未知；未达上限也不能据此证明同步覆盖完整。默认 monthly 是上一完整月，rolling 是近 28 日，阶段回顾不是完整月报。需要量化时使用对应确定性工具的有界结果，不把局部记录当整月全量；产品目标指标不是训练月统计。

## 已完成力量训练与容量

健身语境的“容量”默认指已完成训练量，不是最大耐受量。先计算已知部分并回答，再简短指出影响结果的缺项；不要先问用户最大能承受多少，也不要求先确认所有动作或计重方式。

- 调用 `strength-review --from YYYY-MM-DD --to YYYY-MM-DD --limit 100`，范围和 limit 有界（1～100）；它以 `read` scope 调用 `GET /coach/strength-review`。application/intelligence 层执行确定性算术，不写入、不排任务、不触发同步或分析。先近 28 天，指定场次则包含该日及必要的此前历史；只按实际返回范围作答。
- 返回每场、每动作的已记录组次与名义“重量 × 次数”。未知计重方式仍可计算记录数字的名义量，但单独分组；不同单位、计重方式、器械和来源不能混成一个物理总负荷，不擅自乘二或解释为有效肌肉受力。自重动作报告组数和次数，不能记为零负荷或随意补体重。缺数的部分保持未知，已知部分照常给出。
- 此前同动作比较按工具返回的可比条件解释，保留口径与覆盖；未知或冲突的分组不能证明进步。名义容量不是生理容量、最大耐受量、恢复或增肌评分。默认采用记录中的动作名称和负重，结合已知训练目标给整体判断、具体组间观察和 1～2 条下次调整；不把整场 RPE 当每组 RIR，不由时长断言休息过久或编造动作先后。
- 只有需要详细组次时才用 `workouts --from YYYY-MM-DD --to YYYY-MM-DD --strength-details --limit 20`，不能与 `--limit 500` 组合。逐条查看 `strength_details.state`（available/missing/not_applicable）与 `strength_details.strength_sets`。单次 `workouts --id ID --source SOURCE` 读取 `detail.strength_sets`；需要轨迹才用 `--samples`。普通摘要 `strength_sets=[]` 和 `detail_available=true` 都不能证明动作组是否完整，一次缺失不能推广为全部缺失。
- 报告与详情不一致时核对各自日期和已保存内容，不把报告反写成事实，不让用户重填可能已在云端的数据。缺记录先按“缺数据先查”节查授权。普通训练回顾不附恢复审计，不为下次结构建议另查当日日报；确有当前安全或恢复问题才补相关查询。

## 跑步近期表现与情景等效

`query running --day YYYY-MM-DD [--include-research]` 是纯快照读取，返回 state/context/trends。默认近 28 天对比前 28 天，不必追问窗口；结合训练结构、同类型、用力、心率、配速、耐力与覆盖解释已保存比较，不把原始中位配速当作体能进步。配速用 min:ss/km，允许有来源的单位换算。`recorded_effort` 保留 session_rpe 的值、来源和场次；`classification_basis=inferred_not_user_goal` 表示 tempo 等分类来自推断，不是实测努力或比赛资格。分类与 RPE/本人自述冲突时先对照场次、区间和来源，不让分类压过自述。

问半马等目标距离时，先读最近成绩，查看较长跑与 10 km 等记录，选择有代表性的一场，不挑异常最快值。用 `race-equivalence --id ID --source SOURCE --target-km 21.0975 [--effort-basis scenario|user_reported_maximal]` 调用 `GET /coach/race-equivalence`（read）。它读取保存记录做确定性换算、不写入：优先精确 elapsed，没有时用 rounded duration；Riegel 指数 1.06 给等效用时，1.04/1.10 给参数敏感性范围。默认 scenario，可对普通轻松跑直给条件情景：“如果这代表全力表现且耐力支持，等效约……”。用户明确该场是全力比赛/测试时才用 user_reported_maximal，本轮声明不是反馈保存。情景等效不等于实际预测，敏感性范围不是个人置信区间；没有依据的个人置信度、真实比赛目标和生理能力保持未知，不虚构。局部缺口只影响对应解读，不因为 CS 未验证就拒绝条件换算。

CS、fixedHR、durability 已有研究模型，但仍是 shadow_only / unvalidated，不是正式评分或已验证预测；不能说系统没有模型。仅显式 `--include-research` 时读取研究内容，不为得到分数重跑失败模型，也不让研究候选控制训练强度。

## 缺数据先查，再按服务器策略操作

- 先读相关报告状态、训练详情和覆盖，已有结果足够就回答。`coach-policy` 是 `GET /coach/policy` 的纯读取（read），含 enabled、expires_at、revision、max_requests_per_hour、max_inflight、allow_refresh、allow_analyze、today、timezone 与有效授权。策略来自服务器拥有的文件，缺失或无效默认拒绝；模型不读写策略文件、不创建影子授权。有效 grant 内不反复征求同一授权，用户口头要求也不能绕过服务端拒绝。
- Hermes 仅持有 `read+coach+feedback`，没有 sync/analyze/manage scope。主动 POST 只用 `coach-request refresh-workouts --day YYYY-MM-DD --key-file <私密持久路径>` 或 `coach-request analyze --day YYYY-MM-DD --key-file <私密持久路径>`。只有今天或昨天实际缺失的源数据才 refresh：一个服务时区的明确日历日、Zepp 训练、至多 4 条明细，不是指定单场；不得扩大用户只许单场的范围。陈旧或缺失的已保存分析可按授权 analyze 最近 28 天；已有动作组、缺标签、未来观察或失败模型不能靠反复操作解决。
- 服务器默认根预算 3 次/小时、最多 1 个 inflight，分析 lookback 28 天，以 policy 为准；这是根请求预算，不是 CPU 或子任务数量预算。新请求受理时检查授权，过期或撤销不取消已入队任务。每个用户问题最多 2 个根操作、总共最多 3 次 job 查询；同日期同目的不重复提交，refresh 已产生所需分析时不另补 analyze。
- 403、429、网络错误或需登录就停止，不回退到宽权限 sync/analyze 或扩大回填；不换日期或 key 绕限。响应不确定保留同一 key 文件和相同请求，恢复时复用，不自动重试写入。受理不是完成，成功后回读；未完成也先回答已取得部分。
- 静默上下文传给本次运行和子任务：本链路不得 enqueue、rearm、repoint 待投递报告，不修改既有投递指向；仍可保存源数据与分析。独立定时未来报告可以使用新事实，不是永远冻结报告。明确反馈仍可触发正常分析及其投递，不承诺所有写操作静默。

## 明确意图与写入

讨论、建议、说累、赞同、发送/打开/沉默都不是记录授权；总结、比较或“分析一下”不新增权限。用户明确要求记录才执行 `feedback --key-file <私密持久路径>`，JSON 从标准输入传入，日期取用户本地 calendar_day，至少一项实际输入：feeling_score/physical_fatigue/mental_state/muscle_soreness 为 1～5 整数，notes 最多 500 字；session_rpe 为 1～10，并同时关联已完成的 workout_id 与 workout_source。不猜评分，范围建议不是已完成 repetitions，`12 / 10 / 8` 不能改成 `3 × 10`。成功后用 `query feedback --start YYYY-MM-DD --end YYYY-MM-DD` 核对；反馈已创建影响范围分析，无需补发 analyze。

资料、偏好、目标、建议完成和事件确认也需明确意图与对应 scope；Hermes 无 manage 不执行。非 Hermes 有权限客户端使用既有 action 白名单、标准输入 body、当前 revision 和持久 key 文件，confirmed 由用户输入明确提供。客户端用持久 `--key-file` 保存 Idempotency-Key，绑定 method/path/body，不得换请求复用；不确定响应沿用原键，冲突先回读。不借知识包另存记录，只有真实成功才能说已记录。
