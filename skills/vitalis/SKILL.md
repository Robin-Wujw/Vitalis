---
name: vitalis
description: Retrieve Vitalis facts, use bounded deterministic coaching tools, complete grounded answers, and record explicitly authorized feedback.
---

# Vitalis 数据、工具与权限

使用[独立 HTTP 客户端](scripts/vitalis_api.py)，只需 Python 3.11+ 标准库，不依赖仓库或工作目录。完整参数见[API 操作清单](references/api.md)，按需定位相关操作章节和 schema，不整份读取。教练知识与计划取舍由 `healthy-fitness-coach` 提供；本 Skill 负责取数、确定性工具、权限与结果真实性。

## 配置与事实

所有业务操作通过 API 完成；安装仓库时可用 `vitalis api ...`，独立 Skill 使用本目录脚本，两者来自同一源码。新增 JSON 接口可用 `request METHOD relative-path --param NAME=VALUE`，POST/PATCH/PUT 从 stdin 读取 JSON，幂等写入带 `--key-file`；通用入口不扩大用户授权。禁止改用服务器数据库、内部 Python 用例或 Zepp 凭据绕过 API 权限。

`catalog` 查看字段、单位与原始流资格；睡眠和指标的按需读取见“报告与新增数据”。晨报时机用 `request GET health/wake-review --param from=D --param to=D`；实验比较用 `request GET intelligence/research/readiness --param from=D --param to=D`，只解释为未验证实验结果。实验重算需要 analyze scope；Hermes 不通过通用 request 绕过 coach 授权，不能把用户希望查看研究结果当成授权扩展。

- 运行 `python <Skill目录>/scripts/vitalis_api.py <命令> ...`。`VITALIS_API_BASE_URL` 是无路径、凭据、查询或片段的 origin，非本机必须 HTTPS，不跟随重定向。令牌优先由 `VITALIS_ACCESS_TOKEN_FILE` 的绝对私密普通文件读取，每次请求重新加载；不接受符号链接，POSIX 不得他人可读或组可写。未配置文件才用 `VITALIS_ACCESS_TOKEN`；文件配置失败不回退。令牌不放命令行、日志或聊天，`X-User-Id` 不选择身份。
- 区分 API 事实、用户自述、工具计算和教练建议。保留来源、单位、计重方式、观测时间、日期口径、窗口、样本与覆盖；不同用户、设备、来源或单位不混成同一事实。缺失不是零，未知训练日不是休息日。`synthetic`/`mock` 保留测试标签，不把合成来源说成设备实测；每次引用合成个人事实时在回答开头注明“合成演示数据”。资格信息用于判断，只有影响答案时用一句人话说明，不逐项输出内部审计。
- 确定性工具可以计算有来源的记录量、比较与条件情景，单位换算和格式化也允许。把工具返回的名义量与真实生理能力分开，不虚构恢复分数、趋势、因果、肌肉收益或个人置信度；研究或厂商 readiness/Charge 不升级为正式结论。教练判断不冒充后端已验证安排。
- 陈旧只限制依赖该快照的结论：注明相关日期后继续回答历史事实、一般知识和有依据的条件性建议，不用旧快照证明当前恢复。`INSUFFICIENT_DATA`、未完成任务和错误不是零或当前结论。一般知识无需个人 API；对话已经取得且仍适用的事实可以复用。
- 需要个人数据的任务在同一轮取数、调用工具并给出结果，不以“可以查询”代替执行。服务器推送继续使用确定性引擎、已保存报告和固定渠道排版，不依赖模型在线；聊天草案不改写快照、不自动进入推送，也不自动成为已接受计划。没有计划保存接口就不能声称已保存。

## 按问题读取

| 任务 | 最少相关读取 |
| --- | --- |
| 昨晚睡得怎样、晨报说了什么 | `report morning --day YYYY-MM-DD --view`；只有要核对时序或缺项时才补睡眠记录。 |
| 当前适合怎样练 | `report daily --view` 读已保存建议；必要时 `--state` 或 `status` 核对新鲜度和覆盖。 |
| 今天完成情况、日报明细 | `report daily --day YYYY-MM-DD --view`；解释来源或目标再读取同日期完整日报。 |
| 晚间回顾、晚报说了什么 | `report evening --day YYYY-MM-DD --view`；按返回的截止时刻解释当天记录，不把尚未结束的当天当完整日比较。 |
| 上周变化与下周重点 | `report weekly --day YYYY-MM-DD --view`，`--day` 是查询锚点：问上周时传今天，不能传上周日（那会再退一周）。按返回的 `period_start` / `period_end` 解释上一完整周；当前周未结束不能借此回答本周至今。 |
| 具体训练步骤、解释减量 | 按需读完整 `report daily --day YYYY-MM-DD`；`query explain --day YYYY-MM-DD` 读真实触发事实与 `evidence_refs`。`query context` 的标题不是完整步骤，助手草案不用伪造系统证据。 |
| 目标、偏好、近期安排 | 只补确实未知的 `query profile`、`query training-preferences`、`workouts`；已知目标、时间和限制不重复询问。 |
| 完成了多少力量训练、同动作比较 | 使用下节 `strength-review`；需要原始组次再读力量明细。 |
| 近期跑步、比赛距离等效 | 使用跑步节的快照与 `race-equivalence`，不要把情景当实际预测。 |
| 主观感觉与明确记录 | 讨论感受不写入；明确要求记录才用 `feedback`，不替用户猜评分。 |

`status` 查同步覆盖，`job ID` 查任务；`report` 支持 daily/morning/evening/weekly/monthly/weekly-briefing/monthly-briefing。`--view` 是公开 blocks，`--state` 是新鲜度、last-good、任务与下一步，两者互斥。`query` 只用客户端白名单，按需组合 `trends`、`training-responses` 等相关读取。查询不会启动同步或分析。`status=error`、401/403、网络故障和 `status=snapshot_missing` 分别处理；后者仅表示指定日期无报告，不能自动改日期或编造结果。

默认“最近”取近 28 天；跑步比较前 28 天。日期按用户本地语境，部署默认 `Asia/Shanghai`，API 或用户给出不同口径时保留差异，只有实质歧义才澄清。本月至今取本地月初至截止日，用下节区间统计；需要场次时读 `/workouts` 并沿 `next_offset` 分页，保留 `coverage.truncated` 的截断提示。读完分页也不能据此证明同步覆盖完整。默认 monthly 是上一完整月，rolling 是近 28 日，不是完整月报；产品目标指标不是训练月统计。

## 按 MCP 能力查询已保存数据

用 `request GET <路径> --param from=YYYY-MM-DD --param to=YYYY-MM-DD`；路径省略 `/api/`。先选一个与问题最相关的接口，已有数据足够就回答，不遍历全部目录：

| 问题 | 路径与参数 |
| --- | --- |
| 任意区间总体回顾 | `health/period-summary`，各部分与来源分别给窗口、样本和覆盖。 |
| 运动量、区间纪录、游泳或跑姿趋势 | `workouts/statistics`，`sport`/`family` 过滤，`group_by=week|month|year|sport|sport_family|none`；`metric` 从 `workouts/catalog` 选。 |
| 运动圈段、分段、轨迹 | `workouts/ID/sections`，`source=zepp`；按需重复 `--param include=summary --param include=laps`，另有 splits/streams/gps。GPS 可能是未解码原文。 |
| 日活动及小时步数 | `health/daily-summary`，按需 `include_hourly=true`；分钟最多 31 天，用 `include_minutes=true`。区间总量读取各来源 `summary.指标[].summary.total`，周期摘要在 `activity.summary` 下，不把不同设备相加。 |
| 某日心率、区间睡眠 | `health/heart-rate --param day=YYYY-MM-DD`，按需 `include_series=true`；`health/sleep` 返回完整睡眠、午睡及睡眠窗呼吸/HRV。 |
| HRV、压力、血氧、PAI、Charge、准备度、每日目标 | `health/summary/CATEGORY`，类别为 hrv/stress/spo2/pai/body_charge/readiness/daily_goals/resting_heart_rate/respiratory_rate；按需 include_samples=true。 |
| 体重体脂、乳酸阈值 | 全部已保存体成分用 `request GET health/body-composition --follow-cursor --param limit=200`，支持 `before=YYYY-MM-DD` 和 `from`/`to`；客户端原样传完整游标，不手抄或缩写。乳酸阈值读 `health/training-thresholds`。 |
| Zepp 档案、设备固件、能力范围 | `sources/zepp/profile`、`sources/zepp/schema`；与本人确认的 `query profile` 分开。 |
| Zepp 凭据授权、已保存的到期时间 | `request GET health/token-status`；`query connection-progress` 只查连接/同步进度，不能用它回答凭据到期。 |

体成分完整命令：`python <Skill目录>/scripts/vitalis_api.py request GET health/body-composition --follow-cursor --param limit=200`。最多 50 页、10,000 条完整记录、8 MiB 响应；`state=partial` 时按 `pagination.stop_reason` 说明未读完，续查保留 `pagination.resume_params` 的完整参数。逐页 coverage 保留在 `pages`，读完仅表示此次已保存分页结束，不能证明厂商历史已全部同步。凭据完整命令：`python <Skill目录>/scripts/vitalis_api.py request GET health/token-status`；到期字段为空就回答未保存，不为此额外查档案或进度。

`no_data` 表示本地窗口没有记录，不代表没功能、没发生或零值。先结合 `status` 的取数状态解释；指标目录不是本人数据证明。不同来源、设备、单位、资格保持分组，samples_truncated 仅截样本展示，coverage_scope 只承诺已保存观测。厂商皮温原值、muscleRate、GPS 编码等不猜单位；目标中的 vendor_burning_minutes 是 Zepp 燃脂时间，不是全天运动时长。

日活动总量只累计有唯一记录的观测日，缺日不补零；保留 `coverage` 和 `total_qualification`。`ambiguous_daily_records` 表示同来源同日有多个候选，总量为未知，不从均值或明细另算一个确定总量。体成分每页是完整测量记录，`supported_metrics` 只是能力目录；体脂未出现在 measurements 中时仍是缺失。

健康摘要支持超过 730 天的日期范围，整体统计由 API 按完整窗口计算；请求受实际记录、样本和响应大小预算约束，超限返回 413。可以缩小日期范围回答局部结果，但必须保留各自窗口，不能自行拼接均值或趋势并宣称已回答原窗口的整体统计。分钟活动仍最多 31 天，日期范围长不等于可一次读取全部分钟明细。

睡眠总时长/深睡/REM 总量读取 `summary` 的 qualified total；延迟和睡眠静息心率用各自分组均值，评分和效率不累加。阈值变化读 `event_changes`/`workout_changes`，不混合厂商事件和单场估值；端点多候选或不足两个观测日保持未知。日活动 `activity_modes` 是原始模式码的阶段累计，不猜运动名；缺失与重叠按 `total_qualification` 解释。Charge 的 `daily_max_trend` 表示每日峰值趋势，与原有日均值趋势区分；首尾冲突值保持未知，不升级为训练许可。ODI validity code 仅报告原始码与未知语义，不把它当有效/无效或连续分数。

训练 `series.family_partitions` 按来源和已知运动家族汇总趋势；`best` 是区间极值对应的场次，不是更健康的结论。认证的 `token_expires_at` 可为 null，国家码未保存时保持未知，不从区域主机猜国家，也不为补此字段越权调用原始接口。

原始厂商 GET 是 `POST sources/zepp/raw-requests` 加持久 key，JSON path/params 从标准输入传入，再读返回 result_url；仅专门授予 `source_raw` 的客户端使用。Hermes 当前无此权限，不试探原始接口、不用它替代 coach 操作；原文可读也不等于已解码或可做健康判断。

## 报告与新增数据

先读对应日期的 `report morning`、`report daily`、`report evening` 或 `report weekly --view`，复用已保存的判断和补充事实，不把全部数据目录逐项查询一遍。需要当前安排、报告缺失或数据看起来不一致时补 `--state`：核对目标日、`analysis_run_id`、新鲜度与 `last_good_snapshot`；跨请求版本不一致时回读一次，不把新事实拼入旧决策并称其已采用。周报使用返回的 `period_start`、`period_end`，比较只引用报告已保存的窗口、样本数与 coverage；不在聊天客户端重算另一套周平均或达标率。

普通晨报、日报、晚报、周报回答默认不展示 ATL/CTL/TSB，即使 API 返回这些值，也不追加厂商负荷表或两套口径的说明。围绕睡眠、活动完成情况、已保存训练建议和确有依据的变化作答。结构化事实仍保留，不把省略展示说成缺数据；用户明确询问厂商负荷时，才按下表只解释被问到的指标，不自动展开六个字段。不为 `_total` 猜测中文口径；需要区分时保留原字段名，并说明两者关系尚未核实。

问目标或步数、热量、活动完成情况，第一次取数必须是 `report daily --day YYYY-MM-DD --view`（需要审计则完整日报）。报告已包含 recorded_context 或公开事实中的目标与实际，就直接采用，不再逐项查询 metrics；只有回答确实需要且报告缺失的字段才补查。

交付个人报告回答前检查：没有已保存个人参照就不添“正常、偏高、偏慢”等评价；“单个指标不能说明不能练”也不能改写为“可以练”。Zepp TSB 只复述厂商记录及决策资格，不从符号解释近期负荷高于基线、疲劳或适应。拒绝操作时只给实际支持的 API/CLI 下一步或所缺 scope；不得描述尚未确认的 UI，不说“去 Vitalis 应用内手动触发/更新”，不虚构策略开关、管理界面或产品入口。

按问题补查最小范围，`catalog` 只解释支持的字段与资格，不证明当前账户有该数据；实际是否可用看记录、`status` 覆盖和截止时刻。报告采用核实的 Zepp 账户结果；原始 API 返回多个来源时核对 `source_scope=user_fused` 与具体资格，设备或未知记录可单独回答，但不能补成账户报告事实。

| 需要补查什么 | API 读取与解释 |
| --- | --- |
| 睡眠时序 | `sleep-records --from D --to D` 用醒来所属 `sleep_day`。`sleep_latency_minutes` 是入床至入睡，`time_in_bed_minutes` 是在床时长，`sleep_efficiency_percent` 是已计算效率；入床不等于入睡，醒来不等于离床。字段缺失保留未知，不从分期图估值或诊断失眠。回答个人记录只引用已保存个人参照；没有参照就报数字，不自行补“10～20 分钟正常”、效率高低或精度提升的评价。 |
| 活动目标 | `metrics step_goal --daily --from D --to D`；同样支持 `calorie_goal`、`active_minutes_goal`，日期为 `activity_day`。`active_minutes_goal` 是 Zepp 燃脂时间目标，只与 `vendor_burning_minutes`（原字段 `totalBurningDuration`）对照：按设备心率区间记录，不等于训练总时长，也不证明实际减脂；不能拿 `active_minutes` 或训练分钟替代。这些是设备设置目标，热量目标不是实际消耗，更不是饮食预算；进度优先读报告已保存的同日实测对照，目标存在不代表已完成。日内只说已记录与目标，不由当天时刻判断进度偏慢或提前判定失败。 |
| 明确追问厂商负荷 | 已读报告有对应事实就复用；确需补查时用 `metrics vendor_atl --daily --from D1 --to D2`，按问题换 `vendor_ctl`、`vendor_tsb`。先用人话说明：负荷是设备对活动量的估计，不是重量或卡路里；Zepp App 定义 ATL 为近 7 天活动量加权平均、CTL 为近 42 天加权平均、TSB 为 CTL−ATL。解释个人数值时保留 Zepp 来源、记录日期、`vendor_load` 单位和原始负值；加权细节及 `_total` 与统计选项的映射仍未核实，不能由正负推断个人疲劳或训练许可。不相加每日 ATL/CTL/TSB，不拿 TSB 或 Charge 当 Vitalis 恢复分数和加量许可，不解释成“疲劳大于适应”“轻度负值”；今天怎么练仍取已保存 Vitalis 决策与明确限制。 |
| 体重记录 | `metrics weight --from <带时区时间戳> --to <带时区时间戳>`，默认 raw，例 `2026-02-01T00:00:00+08:00`。不加 `--daily`；同样可查 height/bmi。说清测量日期与单位，不把旧测量当今天体重；只有两次记录时只描述两个时点，不称持续趋势、减脂或增肌，也不推算饮食热量。 |
| 训练精确时间 | 先用报告或 `workouts --id ID --source SOURCE`，按原字段区分移动、暂停、总用时；配速比较保留用时口径。暂停不等于力量组间休息，缺失分量不做差补齐。 |

指标保留 `source`、`source_scope`、`qualification`、观测时间和获取时间；`truncated=true` 时缩小窗口或明确结果不完整。`retained_raw` 仅说明保留了未解码数据，不是可用评分。`catalog` 是允许的 read 查询；不能仅凭 readiness_raw 名称断言目录不在白名单或无权读取，查看目录资格与解码状态后解释缺口。缺失一项不阻断其他已验证事实。

把源数据缺失与分析陈旧分开：源记录已到、报告尚旧时，按 coach-policy 决定是否申请 analyze。没有 analyze scope 不等于不能申请分析：先读 `coach-policy`，有效且 `allow_analyze=true` 才用 `coach-request analyze`；拒绝就如实说明，不先许诺可执行，也不让口头同意代替服务器 grant。`refresh-workouts` 不能补取睡眠、体重或日目标。相应数据缺失就解释实际缺口，不能换用通用 sync、反复重算或要求用户重新填写可能尚未同步的数据。成功任务后只回读相关结果，未完成保留有日期的 last-good 并说明正在更新；不把任务受理当更新完成。

## 已完成力量训练与容量

健身语境的“容量”默认指已完成训练量，不是最大耐受量。先计算已知部分并回答，再简短指出影响结果的缺项；不要先问用户最大能承受多少，也不要求先确认所有动作或计重方式。

- 调用 `strength-review --from YYYY-MM-DD --to YYYY-MM-DD --limit 100`，范围和 limit 有界（1～100）；它以 `read` scope 调用 `GET /coach/strength-review`。application/intelligence 层执行确定性算术，不写入、不排任务、不触发同步或分析。先近 28 天，指定场次则包含该日及必要的此前历史；只按实际返回范围作答。
- 返回每场、每动作的已记录组次与名义“重量 × 次数”。未知计重方式仍可计算记录数字的名义量，但单独分组；不同单位、计重方式、器械和来源不能混成一个物理总负荷，不擅自乘二或解释为有效肌肉受力。自重动作报告组数和次数，不能记为零负荷或随意补体重。缺数的部分保持未知，已知部分照常给出。
- 此前同动作比较按工具返回的可比条件解释，保留口径与覆盖；未知或冲突的分组不能证明进步。名义容量不是生理容量、最大耐受量、恢复或增肌评分。默认采用记录中的动作名称和负重，结合已知训练目标给整体判断与具体组间观察，用户问调整时再给 1～2 条下次建议；不把整场 RPE 当每组 RIR，不由时长断言休息过久或编造动作先后。
- 只有需要详细组次时才用 `workouts --from YYYY-MM-DD --to YYYY-MM-DD --strength-details --limit 20`，不能与 `--limit 500` 组合。逐条查看 `strength_details.state`（available/missing/not_applicable）与 `strength_details.strength_sets`。单次 `workouts --id ID --source SOURCE` 读取 `detail.strength_sets`；需要轨迹才用 `--samples`。普通摘要 `strength_sets=[]` 和 `detail_available=true` 都不能证明动作组是否完整，一次缺失不能推广为全部缺失。
- 报告与详情不一致时核对各自日期和已保存内容，不把报告反写成事实，不让用户重填可能已在云端的数据。缺记录先按“缺数据先查”节查授权。普通训练回顾不附恢复审计，不为下次结构建议另查当日日报；确有当前安全或恢复问题才补相关查询。

## 跑步近期表现与情景等效

`query running --day YYYY-MM-DD [--include-research]` 是纯快照读取，返回 state/context/trends。默认近 28 天对比前 28 天，不必追问窗口；结合训练结构、同类型、用力、心率、配速、耐力与覆盖解释已保存比较，不把原始中位配速当作体能进步。配速用 min:ss/km，允许有来源的单位换算。`recorded_effort` 保留 session_rpe 的值、来源和场次；`classification_basis=inferred_not_user_goal` 表示 tempo 等分类来自推断，不是实测努力或比赛资格。分类与 RPE/本人自述冲突时先对照场次、区间和来源，不让分类压过自述。回答进步问题时选最能支持判断的 2～3 条证据，每项限制放在对应证据旁一次说清，答完不重复总结。不把已跑距离概括成能力上限（例如“目前能跑 5～6 公里”）；漂移缺失若不影响已作出的判断就不另起段补充。用户未问如何训练或记录时，不附通用训练或记录建议；明确风险仍须及时处理。

问半马等目标距离时，先读最近成绩，查看较长跑与 10 km 等记录，选择有代表性的一场，不挑异常最快值。用 `race-equivalence --id ID --source SOURCE --target-km 21.0975 [--effort-basis scenario|user_reported_maximal]` 调用 `GET /coach/race-equivalence`（read）。它读取保存记录做确定性换算、不写入：优先精确 elapsed，没有时用 rounded duration；Riegel 指数 1.06 给等效用时，1.04/1.10 给参数敏感性范围。默认 scenario，可对普通轻松跑直给条件情景：“如果这代表全力表现且耐力支持，等效约……”。用户明确该场是全力比赛/测试时才用 user_reported_maximal，本轮声明不是反馈保存。情景等效不等于实际预测，敏感性范围不是个人置信区间；没有依据的个人置信度、真实比赛目标和生理能力保持未知，不虚构。局部缺口只影响对应解读，不因为 CS 未验证就拒绝条件换算。

CS、fixedHR、durability 已有研究模型，但仍是 shadow_only / unvalidated，不是正式评分或已验证预测；不能说系统没有模型。仅显式 `--include-research` 时读取研究内容，不为得到分数重跑失败模型，也不让研究候选控制训练强度。

## 缺数据先查，再按服务器策略操作

- 先读相关报告状态、训练详情和覆盖，已有结果足够就回答。`coach-policy` 是 `GET /coach/policy` 的纯读取（read），含 enabled、expires_at、revision、max_requests_per_hour、max_inflight、allow_refresh、allow_analyze、today、timezone 与有效授权。需要核对操作资格时直接 GET，不问“要不要我查 policy”，也不先许诺能执行。策略来自服务器拥有的文件，缺失或无效默认拒绝；模型不读写策略文件、不创建影子授权。有效 grant 内不反复征求同一授权，用户口头要求也不能绕过服务端拒绝。
- Hermes 仅持有 `read+coach+feedback`，没有 sync/analyze/manage scope。主动 POST 只用 `coach-request refresh-workouts --day YYYY-MM-DD --key-file <私密持久路径>` 或 `coach-request analyze --day YYYY-MM-DD --key-file <私密持久路径>`。只有今天或昨天实际缺失的源数据才 refresh：一个服务时区的明确日历日、Zepp 训练、至多 4 条明细，不是指定单场；不得扩大用户只许单场的范围。陈旧或缺失的已保存分析可按授权 analyze 最近 28 天；已有动作组、缺标签、未来观察或失败模型不能靠反复操作解决。
- 服务器默认根预算 3 次/小时、最多 1 个 inflight，分析 lookback 28 天，以 policy 为准；这是根请求预算，不是 CPU 或子任务数量预算。新请求受理时检查授权，过期或撤销不取消已入队任务。每个用户问题最多 2 个根操作、总共最多 3 次 job 查询；同日期同目的不重复提交，refresh 已产生所需分析时不另补 analyze。
- 403、429、网络错误或需登录就停止，不回退到宽权限 sync/analyze 或扩大回填；不换日期或 key 绕限。响应不确定保留同一 key 文件和相同请求，恢复时复用，不自动重试写入。受理不是完成，成功后回读；未完成也先回答已取得部分。
- 静默上下文传给本次运行和子任务：本链路不得 enqueue、rearm、repoint 待投递报告，不修改既有投递指向；仍可保存源数据与分析。独立定时未来报告可以使用新事实，不是永远冻结报告。明确反馈仍可触发正常分析及其投递，不承诺所有写操作静默。

## 明确意图与写入

讨论、建议、说累、赞同、发送/打开/沉默都不是记录授权；总结、比较或“分析一下”不新增权限。用户明确要求记录才执行 `feedback --key-file <私密持久路径>`，JSON 从标准输入传入，日期取用户本地 calendar_day，至少一项实际输入：feeling_score/physical_fatigue/mental_state/muscle_soreness 为 1～5 整数，notes 最多 500 字；session_rpe 为 1～10，并同时关联已完成的 workout_id 与 workout_source。不猜评分，范围建议不是已完成 repetitions，`12 / 10 / 8` 不能改成 `3 × 10`。成功后用 `query feedback --start YYYY-MM-DD --end YYYY-MM-DD` 核对；反馈已创建影响范围分析，无需补发 analyze。

只要求记录疲劳但未给评分时，仅补问 physical_fatigue 的 1～5 分，不同时索取整体感觉、精神和酸痛；用户只希望保存原话时可记录其明确提供的 notes，不把文字转换成猜测分数。

资料、偏好、目标、建议完成和事件确认也需明确意图与对应 scope；Hermes 无 manage 不执行。非 Hermes 有权限客户端使用既有 action 白名单、标准输入 body、当前 revision 和持久 key 文件，confirmed 由用户输入明确提供。客户端用持久 `--key-file` 保存 Idempotency-Key，绑定 method/path/body，不得换请求复用；不确定响应沿用原键，冲突先回读。不借知识包另存记录，只有真实成功才能说已记录。
