# Vitalis 健康智能架构 v7.0

[English](ARCHITECTURE.en.md)

## 1. 系统边界

Vitalis 分为三层。只有健康智能层会计算健康状态或训练决策。

```text
Assistant
  Hermes Skill: Read / Analyze / Act + Chinese rendering
                              |
Intelligence                  v
  Sync -> Analyze command -> immutable AnalysisRun / snapshots
                              |
  Quality -> Baseline -> Features -> Trends -> Event Lifecycle -> Monthly
                              |
  Concurrent Planner -> Recommendation -> Workout -> Response -> Association -> Personal Model
                              |
Data                          v
  Zepp connector -> normalized records -> SQLite/PostgreSQL
```

Hermes 不是分析引擎。它可以从结构化的 Vitalis 响应中选择字段并组织措辞，
但不能计算趋势、每周聚合、分数、阈值、训练响应、恢复时间、每月聚合、相关性、
个人模式、置信度或替代建议。其每日解释路径读取一个已持久化的
DecisionExplanation 投影；它不能创建快照、同步数据，也不能用前一天的结果或
通用建议替代缺失的结果。

## 2. 数据层

`vitalis/connectors` 对厂商载荷进行身份认证、同步和规范化。分析层仅读取
规范化记录：

- 每日记录：睡眠、活动和训练。
- `metric_samples`：带时间戳的 HR、RMSSD、SDNN、睡眠 HRV/RHR、就绪度组成项、
  Charge、设备范围的压力观测值、SpO2 以及其他受支持的测量值。
- `daily_metrics`：就绪度、压力、呼吸频率、PAI、ODI 和乳酸阈值字段等稀疏的厂商事实。
- `workouts` 和 `workout_metric_samples`：规范化的训练摘要、带版本的详情元数据、
  设备心率区间边界，以及类型化的训练 HR、速度、等效配速、步频、步幅、距离、
  海拔、跑步功率、触地时间、垂直振幅和垂直步幅比观测值。
- `dense_data_files`：按设备划分的 `SEC_HR` 文件索引、解码状态和样本数。
  解码后的秒级值作为普通的设备范围 `heart_rate` 指标样本存储。

原始测量值和厂商分数与 Vitalis 派生状态保持分离。
`all_day_stress.data` 使用其明确的 UTC 时间戳提供带时间戳的厂商压力观测值；
缺口保持缺失，并且不会推断任何类别阈值。系统会在配置的本地窗口周围扩展
其 UTC 日期请求，然后在持久化前将样本裁剪到原始的半开区间。厂商提供的每日
平均值、最小值/最大值和类别占比仍作为单独的 `daily_metrics` 保存。
两种形式都不会改变恢复状态或训练决策。`ahi_readiness` 和 `afib_readiness`
是厂商就绪度组成项分数，并非 AHI 或 AFib 诊断。

训练摘要保留原始的厂商数字类型 ID。`sport_types.py` 将当前公开的全部 120 种
Zepp OS 模式，以及另外两种公开的旧版 Huami 云历史模式，映射到稳定代码、
准确的中文标签、宽泛类别和训练系列。已知的数字定义具有较高的识别置信度。
缺失或未知的 ID 会保持明确且可审计，而不会继承端点标签或被猜测为已知活动。

带时间戳的测量值仍以 UTC 存储。每日健康智能按配置的应用时区
（`VITALIS_TIMEZONE`，当前为 `Asia/Shanghai`）对其分组。睡眠时钟时间使用
厂商提供的偏移量，训练摘要则归入训练开始时所在的本地日期。

`workouts` 是规范的训练事实表。Zepp 运动页面只会更新或插入规范训练身份；
随后，系统会从该配置时区区间内存储的所有训练中重建每个受影响的本地日期。
因此，`training_records` 是派生的每日摘要，绝不是页面级累加器。由于它融合了
每个规范训练数据源，其分析来源为 `canonical_workouts`，而不是厂商名称。
更正训练时间戳会同时重建其原先和新的本地日期。

规范化数据流身份包含来源信息。带时间戳的指标使用 `(user, source,
metric, timestamp, source_scope, device_id)`；稀疏每日指标使用 `(user, source,
date, metric, source_scope, device_id)`。训练详情样本的身份中也包含规范训练数据源，
因此来自不同连接器的相同厂商训练 ID 无法删除或合并彼此的测量值。
详情 API、ProfileLoader、反馈、建议、力量训练确认、训练响应和时间线引用均使用
相同的 `(source, workout_id)` 键。缺失的设备会在内部存储为空的非 null 身份，
以便 SQLite 和 PostgreSQL 应用相同的唯一性语义；但 API 和健康智能边界会将其
作为缺失项公开，而不是将其作为设备名称公开。

Zepp 失败带有机器可读的类型。只有显式的 `not_available` 响应可以视为
不受支持的可选能力；身份认证、网络、服务和厂商响应失败仍然是失败。
空的成功云响应和非空的未识别载荷会保留不同的获取/解析/写入状态。

训练详情仅采用当前契约（`schema_version=4.0`）。在后续同步窗口中，旧版详情
契约的行会分批、限量获取和替换，以免历史升级耗尽整个健康同步预算。
Zepp 增量/时间序列会解码为类型化指标样本。分圈仅保留已验证的序号、时长和
距离语义；暂停保留开始时间和时长。跑步分析从这些规范化数据流中推导移动时间、
每公里配速、心率和海拔。明确的厂商力量训练组数据可以包含动作身份、重复次数、
重量、做功时长和休息时长。空白或无文档说明的厂商字段保持缺失，力量评估载荷
不会被重新解释为动作名称。

## 3. 健康智能层

实现位于 `vitalis/intelligence`：

| 模块 | 职责 |
| --- | --- |
| `contracts.py` | 带版本的分析、建议、响应、个人模型、时间线和上下文契约 |
| `profile.py` | 单个本地用户的加载器、来源信息、目标日事实和确定性质量标志 |
| `baseline.py` | 特定于设备/指标的 7 天和 28 天稳健统计 |
| `analyzers.py` | 睡眠、HRV/RHR、恢复和训练特征提取 |
| `running.py` | 设备/阈值心率区间、步频、功率、跑步动态、可比跑步基线、配速/HR 漂移、分段、训练类型和 7/28 天结构 |
| `strength.py` | 已确认动作、动作和肌肉知识、做功/休息结构、分化状态和肌肉恢复上下文 |
| `decision.py` | 健康优先的跑步/力量训练规划器、安全门控、日程冲突和拒绝作出决策机制 |
| `trend.py` | 按设备隔离的 7/28/90 天趋势和变异性 |
| `events.py` | 持续偏差和周期变化事件检测 |
| `lifecycle.py` | 事件观测以及 DETECTED/PERSISTING/IMPROVING/RESOLVED 转换 |
| `weekly.py` | 每周事实、推断和确定性操作 |
| `monthly.py` | 直接计算的 28 天事实、推断、比较和操作 |
| `association.py` | 按设备隔离的 60/90 天 Spearman 个人关联 |
| `training_response.py` | 按设备隔离的训练前基线和 T+1/T+2/T+3 响应分析 |
| `personal.py` | 按训练系列和具体模式划分的稳健个人响应分布 |
| `context.py` | 有界的 Current/Recent/Trend/Personal 智能体上下文 |
| `timeline.py` | 不含原始样本的类型化摘要时间线 |
| `service.py` | 显式命令、只读查询、用户操作和不可变快照 |

### 3.1 DailyProfile

线格式契约为 `schema_version=12.0`。每项结果分别携带 `analysis_run_id`、
`intelligence_version`、`decision_policy_version` 和 `evidence_version`：

```text
DailyProfile
|- data_quality: status, missing signals, coverage, flags, device validity
|- facts: target-day normalized observations and provenance
|- baselines: 7d/28d robust statistics per metric and device stream
|- features: sleep, device-baseline HRV fusion, dense-HR coverage, RHR, recovery, training
|- trends: device-isolated 7/28/90-day period features
|- events: persistent or period-change observations
|- states: sleep, recovery, training load, Chinese labels
|- decision: state action, evidence, and a dated concurrent ActionPlan
|- evidence_refs
`- metadata: identity and product-policy versions
```

`DailyProfile.report_context` 是数据时效和覆盖边界的显式字典，包含
`as_of`（ISO UTC 时间）、`timezone`、`target_date`、`target_day_complete`、
`training_history` 和 `latest_observations`。其中 `training_history` 固定报告
`status`（`COMPLETE`/`PARTIAL`/`UNKNOWN`）、`verified_days`、`last_synced_at`
和 `prior_7d_verified`；缺少训练历史不会被解释为零训练或休息。

Vitalis 不提供未经校准的 0-100 恢复分数。厂商就绪度、Charge 和睡眠分数
会标记为厂商上下文，不会成为 Vitalis 结果。

#### 3.1.1 开放健康影子洞察

DailyProfile 12.0、WeeklyProfile 5.0、MonthlyProfile 2.0 和 Agent Context 6.0
公开一个带版本的 `open_health_insights` 数据块。其中包含透明的个人基线就绪度、
稳健的多信号异常筛查、睡眠效率/规律性、用户目标睡眠差距、Banister TRIMP，
以及描述性的 ATL/CTL/TSB。

这些输出严格满足 `shadow_only=true`。它们绝不会进入 `RecoveryFeatures.state`、
`DecisionEngine`、决策置信度、规则 ID 或 ActionPlan。当前决策策略仍为 8.0。
未来的策略若使用这些信号，必须显式划分版本并进行测试。

用户确认的生理信息存储在带修订版本的 `UserProfile` 中。`sex` 和已确认的 HRmax
绝不会根据年龄、训练最大心率、厂商就绪度、乳酸阈值或设备心率区间边界推断。
缺失字段会成为类型化的 Agent Context 问题。当前的 Zepp apptoken 同步不会调用
未经验证的云端档案端点。

开放负荷使用一个数据源/设备的训练心率数据流、同一天的规范 RHR，以及用户确认的
HRmax。没有经验证上游同步覆盖的日历日期仍然是下限估计；训练情况未知的日期绝不会
转换为零负荷休息日。TSB 作为描述性的负荷平衡展示，而不是恢复、竞技状态、
过度训练或受伤风险。

HRV 处理绝不会对不同设备的原始毫秒值求平均。Vitalis 会依据可解释的个人基线、
基线日期覆盖率、当前观测密度和连续性，选择一个规范的同指标数据流；测量部位只作为
夜间 HRV 的最终决胜条件。选中的数据流仅与其自身的稳健 28 天基线比较。
次要数据流仍作为审计证据：一致时不作提示且不会提高置信度，可比的不一致会降低
置信度，但不会替代规范结论。这不同于运动心率；对于运动心率，明确归属并解码的
上臂设备数据流具有更强的外形规格证据。

`second_heart_rate` 使用 Zepp 已验证的索引到文件契约。Vitalis 将每个新文件 ID
解析为签名 HTTPS URL，在不转发 Zepp 令牌的情况下下载小型 ZIP，解码
`DailySecondHeartBeat` protobuf，通过全局一对一时间区间重叠分配将归档条目
映射到已索引设备，并将有效值存储为秒级心率。已解码的文件/设备/时间区间会在
后续同步中跳过。夜间分析只读取已记录的睡眠窗口，并在特征提取前将每台设备归约为
每分钟一个中位数；分析档案不会保留数百万个原始数据点。

### 3.2 数据质量

三个概念有意保持分离：

- `data_quality`：确定性的完整性、覆盖率、来源、查询上限和身份标志。
- `device_validity`：证据元数据；除非附有特定于设备的验证，否则为 `UNKNOWN`。
- `decision.confidence`：基于规则的推断完整性分档，而不是设备测量概率。

目标日缺少睡眠或 HRV 时会明确呈现。引擎绝不会分配默认分值或复用旧的分析结果。

一个厂商身份可以映射到多个本地 Vitalis 用户。加载器会报告
`SOURCE_IDENTITY_SHARED`，并继续仅使用请求指定的本地用户；它绝不会自动合并历史记录。

### 3.3 基线

基线键包含本地用户、指标、数据源、数据源范围、设备 ID、窗口和模型版本。
RMSSD、SDNN、睡眠 HRV 和 RHR 绝不会共享数值基线。Zepp 账户级睡眠 HRV/RHR
摘要（`source_scope=user_fused`，无设备 ID）在可用时是主要报告和决策数据流。
每台设备的数据流保持隔离且可审计；Vitalis 绝不会对其求平均，也不会让其覆盖厂商摘要。

在每个数据流内，高频样本会先归约为每日一个中位数，再统计历史资格。引擎计算：

- 中位数和中位数绝对偏差（MAD）；
- 第 25 和第 75 百分位数；
- 每日最小二乘趋势；
- 样本数、不同日期数和覆盖率；
- 目标日的百分比偏差和稳健 z 分数。

RMSSD 使用自然对数值进行稳健统计，并保留原始毫秒单位的 `reference_value`。
7 天基线要求 3 个不同日期，28 天基线要求 14 个。这些最低要求是带版本的产品策略，
而不是医学事实。

### 3.4 趋势与事件

趋势数据流保留指标、数据源、数据源范围、设备 ID 和单位。每个受支持的
7/28/90 天窗口都会报告当前和先前的中位数、可用时的百分比变化、每日斜率、
MAD 变异性、覆盖率、方向和置信度。缺失日期是缺失的观测，绝不是零值。
档案加载器读取 180 个本地日期，以便同时评估当前及之前的 90 天周期。

健康事件是确定性观测，而不是诊断。HRV、RHR 和睡眠偏差事件要求具有持续性；
训练负荷和活动事件要求存在明确的周期变化；恢复受抑制要求状态引擎给出多信号结果。
稳定的事件身份会依次经过 `DETECTED`、`PERSISTING`、`IMPROVING` 和 `RESOLVED`。
每次分析都会写入不可变的 EventObservation。确认操作是独立的交互时间戳，
绝不会改变生理生命周期状态。

### 3.5 WeeklyProfile 与反馈

WeeklyProfile 5.0 覆盖截至请求日期的滚动七个本地日期，并将其与之前七天进行比较；
`report_context` 同时保留目标日是否结束和训练历史覆盖，未结束目标日必须明示。
其契约将可穿戴设备/聚合 `facts`、Vitalis `inferences` 和确定性 `actions` 分开。
恢复事件优先于通用训练量目标。主观 RPE、身体疲劳、精神状态、酸痛和备注
与设备事实保持独立，在缺失时绝不会推断。Weekly 的睡眠和活动事实分别报告
`previous_available_days`。训练事实报告 `record_days`、`unknown_days`、
`coverage_status` 和 `totals_are_partial`；`rest_days`、`duration_minutes`、
`vendor_load`、`aerobic_minutes` 和 `strength_sessions` 在无法确认时保持 null。

显式分析命令会持久化一个不可变 AnalysisRun，以及不可变的 Daily、Weekly、
Monthly、Training Response、Personal Association 和 Personal Model 快照。
重复分析会创建新的运行，而不会覆盖之前的输出。GET 请求只读取请求日期最近一次
持久化的结果；如果不存在，则返回 404。

### 3.6 MonthlyProfile 与个人关联

MonthlyProfile 精确覆盖截至请求日期的 28 个本地日期，并将其与紧邻的前 28 天
进行比较。它根据规范化的睡眠、活动、训练、训练详情、指标数据流、反馈、趋势和
事件数据重新计算，并非由 WeeklyProfile 快照组装而成。事实、推断和操作保持分离。

Personal Association Engine 在 60 天和 90 天窗口内评估固定且有文档说明的
候选变量对。睡眠时长和训练负荷与次日的 HRV、RHR 和睡眠配对；步数与当日睡眠配对。
每个测量数据流保留指标、数据源、数据源范围、设备 ID 和单位。缺失日期按变量对排除，
绝不会用零填充。Spearman 秩在并列时使用确定性的平均秩。

60 天关联至少要求 30 个配对日期；90 天关联要求 45 个。二者均要求至少 50% 的
覆盖率，并且预测变量和结果存在有意义的变化。结果日期当天的训练会计为潜在混杂因素，
并可能降低置信度。强度分档（`<0.2`、`0.2-0.4`、`0.4-0.6`、`>=0.6`）属于
产品展示策略。每项结果都带有 `association_only=true`；它仅作描述，绝不表示因果关系。

### 3.7 建议、训练响应与个人模型

每个 Daily 决策都有一个与其 AnalysisRun 关联的 RecommendationInstance。
完成操作必须显式地在用户范围内关联到一项已存储的训练；Vitalis 绝不会根据时间戳、
文本或训练类型推断关联。Session RPE 必须使用训练身份，且可感知建议的反馈记录
必须与已完成的关联相匹配。

Training Response v1 将每项符合条件的训练与按设备隔离的 28 天训练前基线，
以及 T+1/T+2/T+3 的 HRV、RHR、睡眠和已关联的主观事实进行比较。未来/缺失窗口
保持明确。响应窗口内的任何其他训练都会将结果标记为受混杂影响。只有当 HRV 已恢复至
接近/高于基线、RHR 接近/低于基线、可用睡眠不低于基线且窗口不受混杂影响时，
才会给出恢复小时数。这是确定性的产品策略，而不是医学恢复模型。

Personal Model v2 按训练系列和具体运动模式对响应分组。它为每个特定于设备的响应
数据流报告中位数、MAD、样本数、符合条件的数量、覆盖率和从覆盖率推导的置信度。
它也只纳入中/高置信度的个人关联。其中不包含 ML、合成压力分数或因果主张。

### 3.8 时间线与智能体上下文

Health Timeline 为分析、建议、训练、反馈、事件转换、训练响应、每月摘要和受支持的
个人关联生成类型化摘要投影。它绝不会复制原始传感器或训练样本。Agent Context 6.0
仅包含有界的 Current、Recent、Trend 和 Personal 层，并设置严格的条目数量上限，
且不嵌入 Daily/Weekly/Monthly 载荷。

### 3.9 特征与决策策略

首选 HRV 数据流必须具有目标日观测值。选择时首先优先使用 Zepp 账户级夜间睡眠
HRV 摘要，然后根据恢复指标偏好（夜间睡眠 HRV、面向睡眠的 SDNN，最后是全天 RMSSD）
和可用的个人基线覆盖率回退。所选指标的每个当前设备数据流仍会显示各自的基线偏差，
但设备间不一致属于审计证据，并不会使可用的厂商摘要失效。Zepp 账户级睡眠 RHR
以相同方式配对，不会被从分钟级心率样本推导的中位数替代。全天可视化有其独立的
仅 RMSSD 数据源策略，绝不会改变恢复指标。

恢复判断至少要求 HRV、RHR、睡眠和近期训练负荷中有两个能依据基线解释的信号。
多信号受抑制可以产生 `RECOVERY` 或 `REST`；恢复良好且负荷较低可以产生
`TRAIN_HARD`。每项决策都会返回规则 ID、驱动因素、局限性和一个操作：

```text
TRAIN_HARD | TRAIN_NORMAL | TRAIN_LIGHT | RECOVERY | REST | INSUFFICIENT_DATA
```

引擎检测偏差并指导训练；它不诊断疾病。睡眠阶段仍仅用于趋势。训练负荷由厂商推导；
已记录的 RPE 可补充每周上下文，但不能替代负荷、已完成的组数/重复次数/重量，
也不能替代可靠的个体化有氧强度分类。

训练内容是确定性引擎输出，而不是模型生成的建议。Decision Policy 8.0 返回一个
`ActionPlan`，其中包含一个主要训练环节，以及至多一个兼容的可选附加项或替代项。
每个训练环节都包含剂量、证据、进阶、停止条件和本地日期到期时间。当前训练库包括：

- Zone 2 跑步：热身、通过谈话测试控制的主要训练、放松、进阶和停止条件。
  在个体心率区间得到验证之前，不会虚构数值型 Zone 2 范围。
- 全身抗阻训练：深蹲、推、拉、髋伸和核心动作模式，包含组数、重复次数、休息、
  保留次数、替代动作和负荷进阶。
- 带明确强度约束的恢复活动和完全休息。

规划器的固定产品目标是健康优先，同时要求跑步和力量训练。用户约束指定每周目标、
跑步/力量训练选择策略、跑步机可用性、恶劣天气跑步后备方案、每周可训练日、
训练时间、经验、器材和疼痛/受伤状态。缺失的约束保持明确，并会降低训练处方的
具体程度；已记录的疼痛或受伤会阻止计划训练。

跑步和力量训练的完成情况会同时与 7 天和 28 天目标比较。当两者都需要完成时，
相对缺口较大的项目成为主要训练；缺口相同时，根据最近一次训练系列交替选择。
轻松跑和上肢力量训练可以作为同一天分开的附加项目，两者之间至少间隔六小时。
质量跑和下肢力量训练互为替代项；过去 48 小时内的质量跑/长跑或腿部训练会阻止
另一次存在冲突的高负荷下肢训练。

用户也可以改为选择明确的跑步/力量训练交替模式。在该模式下，最近一次已识别的
有氧训练会决定下一次选择力量训练，最近一次力量训练会决定下一次选择跑步，
即使每周目标缺口不同也是如此。恢复、可用性、受伤和负荷冲突门控仍然优先。
恶劣天气后备偏好会以确定方式存储，但在配置天气数据源之前不会应用；Vitalis
绝不会虚构天气状况。用户可保留 `BALANCE` 或 `ALTERNATE` 选择。7 日缺口主导选择，
28 日充分历史仅作参考，不补课；若存在冲突则实际降低剂量。训练 prior 7 天覆盖为未知时，
不输出训练处方，但仍可解释睡眠和其他已有信号。生命周期为 `RESOLVED` 的事件不再作为
当前限制。

### 3.10 跑步分析

DailyProfile 12.0 通过 Running Analysis v2 嵌入 `TrainingFeatures.running`。
每次训练会保留距离、时长、推导配速和等效配速、速度和步频中位数、步频变异性、
功率、触地时间、垂直振幅、垂直步幅比、HR 区间时长、心率漂移、检测到的
做功/恢复分段、分类证据、置信度和局限性。

每项训练的六个有效 `heart_range` 边界优先使用。如果缺少这些边界，只有在
个人厂商乳酸阈值 HR 可用时，才会使用已验证的 Zepp 阈值边界计算五个区间。
Vitalis 不会根据年龄估算最大 HR。心率漂移要求至少 20 分钟重叠的速度/HR 详情；
当前半段与后半段速度相差超过 15% 时，不会给出该指标。步频是个人观测值；
不采用通用的 180-spm 目标。训练类型包括恢复跑、轻松跑、稳定跑、节奏跑、间歇跑、
长跑或未分类。这些阈值是带版本的产品策略，而不是医学或执教事实。

当之前 180 天内至少有三次早先跑步与目标距离的差异在 20% 以内时，该训练会携带
由最多十次近期可比跑步生成的基线。配速、平均 HR 和功率比较仍是描述性的个人事实；
不会自动标记为体能改善或退化。

### 3.10.1 同步数据健康状态

`sync_attempts` 和 `sync_chunks` 构成持久执行账本。稳定的分块键使重复规划具有
幂等性；尝试和分块的租约令牌/时期会隔离过时的工作进程；已完成的分块在后续失败后
仍会保留；暂时性失败进入有界的 `retry_wait`；取消和重启恢复均为持久化操作。
调度器处理有界批次，并按最近更新时间对到期尝试排序，因此一次大型历史导入不会
独占所有用户的处理资源。长时间运行的获取/解码/写入操作会在进行期间续期两个租约。

`sync_stream_states` 分别记录每个用户所有的数据源流最近一次获取、解析和写入状态，
以及最近存储的样本时间戳。空的云响应、非空的未识别载荷和一次存储写入是不同状态。
只有在每个数据流都成功或整体明确不可用时，同步才算完成。成功/不可用混合的分块
属于部分完成；可选数据流上的网络、服务、解析或身份认证失败会阻止整体成功，
同时保留之前成功的写入。`GET /health/data-health` 公开此诊断契约，但不公开测量值、
租约令牌、原始错误或私有文件标识符；`GET /health/sync/{attempt_id}` 公开用户范围的
进度，`POST /health/sync/{attempt_id}/cancel` 持久化取消请求。投递和同步请求的 timeout 与 5xx
属于可重试故障；明确的 `reauth` 会阻断继续投递，不能被重试掩盖。CLI 与内置路径
共用同一用户/日期发送锁标记；`partial` 按数据流领域判断，不能把一个领域成功解释为
整个同步完成。

跑步处方会使用这些训练事实。它依据近期高强度训练的时间、下肢冲突、最近一次
可解释的心率漂移、个人阈值 HR 和近期已完成跑步的时长中位数，在恢复跑、轻松跑、
稳定跑、阈值跑和轻松长跑之间选择。时长会在个人历史和配置的时间上限附近进行限制。
近期步频只会作为跑者的自然观测值重述，绝不会作为通用步频目标。

### 3.11 力量训练分析

DailyProfile 12.0 通过 Strength Analysis v1 嵌入 `TrainingFeatures.strength`。
用户可以针对自己拥有的一项力量训练确认动作名称、组数、重复次数、负荷、RPE/RIR、
休息和训练重点。Vitalis 会将已知的中文或英文动作名称规范化为动作模式和肌肉群，
同时保留原始名称作为可审计事实。

明确的厂商组数据和用户确认的动作是精确动作身份的唯一来源。缺少这些信息时，
训练心率或经验证的零距离分圈可以估算做功组数和做功/休息时长，但无法识别深蹲、
卧推或任何目标肌肉。力量训练心率区间仅描述心血管上下文，绝不代表负荷强度。Balance 2 应用的
`strengthSets` 可为字符串或列表；跨层会将整数 `reps` 转为字符串，并保留同剂量
但不同重量/次数的组。未知单位保持未知，不补写 `kg`。本地整场的用户确认数据优先于
供应商组数据；近期 28 日已缓存的力量详情只做有界刷新（每次最多 4 个预算，不能保证
一次覆盖全部），刷新时间记录在 `fetched_at`。当前未核验用户的 3-4 场真实云端详情，
不宣称已获得；上游 ZeppBridge v2.1.0 也没有可直接证明的力量组 decoder。

最近 28 天的数据支持带置信度限制的全身、上下肢、推拉腿和五日分化检测。
只有在存在可识别的轮换时，才会返回下一训练重点。各肌肉的最近训练时间与确认的
酸痛/RPE 保持为独立观测；动作覆盖缺失会使肌肉训练量和恢复信息不完整。

力量训练处方首先查找与下一个已识别重点匹配的最近一次明确训练。找到后，它会复用
动作名称、组数、重复次数、负荷和休息，并让已记录的 RPE/RIR 决定保持原计划还是
允许小幅进阶。没有明确动作时，它会使用具体的动作模式选项；心率做功/休息结构
可以确定训练规模并提供解释，但绝不会提供动作身份。计划训练会携带规则所用的
确切证据引用 ID。

### 3.12 夜间恢复上下文

DailyProfile 12.0 将带时间戳的普通心率与每日指标序列分开保存。对于每个睡眠区间，
引擎会隔离设备数据流，并要求至少覆盖 120 分钟且区间覆盖率达到 50%。它会推导
夜间中位数、滚动五分钟中位数低点、前半段和后半段中位数以及覆盖率。选中的夜间
数据流仅与其自身之前 28 晚的历史比较。

规范 HRV 数据流还会报告最近七天中位数与之前七天的比较结果。分钟级心率和解码后的
秒级心率绝不会被视为逐搏 HRV。当当前夜间覆盖率足够时，夜间心率数据流优先选择
已识别的上臂设备，并只与该设备自身的历史比较。

## 4. API 与助手

健康智能 API 包括：

```text
POST /api/v1/intelligence/analyze
GET  /api/v1/intelligence/daily
GET  /api/v1/intelligence/weekly
GET  /api/v1/intelligence/monthly
GET  /api/v1/intelligence/trends
GET  /api/v1/intelligence/events
GET  /api/v1/intelligence/explain
GET  /api/v1/intelligence/context
GET  /api/v1/intelligence/training-responses
GET  /api/v1/intelligence/personal-model
GET  /api/v1/intelligence/personal-associations
GET  /api/v1/intelligence/timeline
GET  /api/v1/intelligence/recommendations/{recommendation_id}
POST /api/v1/intelligence/recommendations/{recommendation_id}/complete
POST /api/v1/intelligence/feedback
GET  /api/v1/intelligence/feedback
GET  /api/v1/intelligence/training-preferences
PUT  /api/v1/intelligence/training-preferences
POST /api/v1/intelligence/workouts/{workout_id}/strength-exercises?source=
POST /api/v1/intelligence/events/{event_id}/acknowledge
```

每个端点都要求提供 `X-User-Id`。旧的 `/intelligence/daily-profile` 路由已被移除，
而不是保留为兼容性别名，因为系统仍处于预生产阶段。

`/api/v1/health/*` 仍是原始/汇总数据及同步接口。原型路径
`/api/v1/health/today` 和 `/api/v1/analyze` 已被移除，因为应用尚处于预生产阶段，
无需兼容性契约。

`skills/vitalis` 为已持久化的 Daily、Weekly、Monthly、趋势、事件、训练响应、
关联、Personal Model、时间线和上下文提供 Read 工具。Analyze 是显式 POST 工具。
Act 涵盖同步、建议完成、主观反馈和事件确认。每个面向用户的值都来自中文标签或
结构化引擎字段。Hermes 绝不会从一个健康智能契约推导另一个契约。

## 5. 计划任务流程

```text
02:00  sync 7 days -> analyze -> immutable snapshots
09:30-21:30 hourly  sync 2 days -> analyze today's DailyProfile
                     -> incomplete sleep: defer
                     -> complete sleep and unsent: Morning renderer -> PushPlus -> mark sent
22:30                sync 1 day -> analyze today's DailyProfile
                     -> Evening renderer -> PushPlus -> mark sent
```

以上是 Vitalis 内置调度入口。Hermes 的 09:30-21:30 每小时晨间任务和 22:30 晚间任务
是替代入口，不是与内置调度同一套计划；部署时只应由一个入口负责实际投递。

如果当天睡眠记录没有醒来时间，晨间调度器会延后处理，并且绝不会用过时的健康结果
替代。晨间和晚间使用相互独立、按用户和日期划分的发送标记，因此重试和重叠调用
不会重复一次已成功的 PushPlus 发送。晚间报告不受晨间睡眠门控阻止。

晨间渲染器是完整 DailyProfile 之上的确定性展示选择层。它通过 `MorningBriefing
schema_version=2.0` 输出睡眠与身体状态观察、当天可用的跑步/力量上下文、一个结论、具体的
主要/可选操作、简短理由、至多一个可采取行动的事件，以及仅有实质影响的注意事项。
当天尚未有 workout 不是缺项；只有决策所需信号不足才会返回 `INSUFFICIENT_DATA`。
按设备划分的数据流、原始趋势窗口、空信号组、未知安全输入、已通过的检查、规划门控
和通用局限性会留在结构化档案中，而不会复制到每日推送中。

晚间渲染器是独立的确定性视图。它会在有数据时按 `started_at` 报告当天每场实际训练
明细，包括跑步指标以及明确的力量训练组；缺失和未知单位保持缺失，不补写 `kg` 或动作。
它还汇总融合后的每日活动和设备记录的压力，解释最近已完成日期的训练负荷，并以恢复及
次日连续性操作作结。它不会把没有正式训练的一天视为问题，不会仅根据负荷推断就绪度，也不会输出
低置信度的训练类型和心率漂移结论。

两个每日渲染器都会为 PushPlus 生成经过净化的 HTML。健康内容仍是确定性文本契约；
展示处理会转义原始 HTML、转换报告方言，并添加保守的内联样式，即使发送渠道剥离
样式后仍可阅读。日间 HRV 解读有意不纳入每日契约，直至能够满足
`RESEARCH_NOTES.md` 中的活动、姿势、呼吸、昼夜节律参考和覆盖率门控。

`HrvFeatures` 将 Zepp 的不同算法保持分离。恢复值和七夜趋势优先使用 Zepp 账户级
`sleepHRV` 摘要。按设备划分的值仍可供检查，Vitalis 绝不会在数值上将其合并。
缺少账户级摘要时，报告会回退到具有最强个人基线的一个设备数据流。带时间戳的
`HRVRMSSD/real_data` 仍可在结构化档案中供检查，但其仅睡眠时间线不会纳入晚间摘要，
因为它不提供有用的全天信息。稀疏的日间 RMSSD 观测值和厂商压力都不会被重新标记为
连续的全天 HRV 曲线。稀疏 SDNN 仍会存储，但不用于每周展示趋势。

训练 `load_7d`、时长和比较窗口都是截至分析日期的滚动本地日期窗口。因此晚间报告
包含当天已完成的训练；三个比较周是紧邻的、互不重叠的前三个七天窗口。WeeklyProfile
同样覆盖截至请求日期的滚动 7 个本地日期；`report_context.target_day_complete=false`
时必须明示目标日未结束，不能把未结束目标日当作完整记录日。

## 6. 证据边界

- WHO 活动指南提供人群层面的每周上下文，而不是就绪度规则。
- HRV 测量标准支持区分指标和处理 lnRMSSD，而不是专有恢复分数。
- World Sleep Society 建议限定消费级睡眠阶段的解读范围。
- AASM/SRS 提供通用的成人睡眠时长参考。
- IOC 共识支持综合监测负荷/恢复/健康，而不是通用的急性与慢性负荷比阈值。
- ACSM 2026 年抗阻训练立场声明支持渐进式主要肌群抗阻训练和可调整的处方变量，
  但在缺乏个人力量历史的情况下，不支持自动给出绝对重量。
- 并行训练证据支持谨慎安排有氧/力量训练，但精确的同日间隔和 48 小时下肢冲突窗口
  仍属于 Vitalis 产品策略。
- RPE/RIR 证据支持带有经验相关局限性的引导式主观用力反馈；它不能精确替代负荷、
  组数、重复次数或设备观测值。

目前尚未找到针对 Balance 2 或 Helio Strap 具体型号的独立验证，因此它们的
`device_validity.status` 仍受证据限制。上臂运动研究无法证明 Helio 的夜间 HRV
准确性，厂商宣称的每秒采样也不会成为准确度权重。

## 7. 当前范围

已实现：不可变 AnalysisRun 快照、命令/查询分离、带版本的 Daily/Weekly/Monthly、
建议/训练/反馈身份、按设备隔离的 Training Response v1、Personal Model v2 稳健分布
及受支持的个人关联、事件生命周期观测、类型化 Timeline、有界分层 Context、
确定性质量/来源、7/28 天基线、7/28/90 天趋势、可解释决策、122 个公开训练 ID、
中文展示契约、跑步和力量训练分析、结构化行动计划、计划分析，以及精简的 Hermes
Read/Analyze/Act 工具。

尚未实现：不受限的探索性相关性发现、分钟级压力负荷、Energy Dynamics、
身体成分/BP 健康智能、预测或医疗警报。这些功能需要明确的新契约和验证，
而不能依赖 LLM 推断。
