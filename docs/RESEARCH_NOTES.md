# 研究笔记

[English](RESEARCH_NOTES.en.md)

本文档记录可能为未来 Vitalis 契约提供参考的本地研究发现。本文档本身并非产品规范。代码变更仍需各自的 `SYSTEM.md` 检查清单、测试、文档和交付提交。

## 2026-08-29：运动处方与设备证据

### 范围

最新实现已经提供 DailyProfile 4.0、Decision Policy 4.0、跑步分析、力量训练分析，以及健康优先的并行 `ActionPlan`。本轮审查旨在确认当前证据库是否足以支持下一次迭代中的以下内容：

- 抗阻训练处方细节；
- 跑步和力量训练均需进行时的日程安排；
- 基于 RPE/RIR 的主观反馈；
- Balance 2 / Helio Strap 设备和 Zepp OS 功能声明。

### 已审阅来源

主要或接近一手的来源：

- WHO 2020 physical activity guidance:
  <https://www.who.int/publications/i/item/9789240015128>
- WHO adult recommendation summary:
  <https://www.ncbi.nlm.nih.gov/books/NBK566046/>
- ACSM 2026 resistance-training position stand:
  <https://doi.org/10.1249/MSS.0000000000003897>
- Wilson et al. 2012 concurrent training meta-analysis:
  <https://pubmed.ncbi.nlm.nih.gov/22002517/>
- Schumann et al. 2022 updated concurrent-training meta-analysis:
  <https://pmc.ncbi.nlm.nih.gov/articles/PMC8891239/>
- Zourdos et al. 2016 RIR-based RPE scale:
  <https://pubmed.ncbi.nlm.nih.gov/26049792/>
- Helms et al. 2016 RIR practical application:
  <https://pmc.ncbi.nlm.nih.gov/articles/PMC4961270/>
- RPE / movement-velocity systematic review:
  <https://pubmed.ncbi.nlm.nih.gov/38910451/>
- Russo et al. 2026 RIR accuracy systematic review:
  <https://doi.org/10.1080/10833196.2025.2564026>
- Zepp OS HeartRate API:
  <https://docs.zepp.com/docs/reference/device-app-api/newAPI/sensor/HeartRate/>
- Zepp OS Workout API:
  <https://docs.zepp.com/docs/reference/device-app-api/newAPI/sensor/Workout/>
- Zepp OS Side Service Fetch API:
  <https://docs.zepp.com/docs/reference/side-service-api/fetch/>
- Zepp OS System Events:
  <https://docs.zepp.com/docs/guides/framework/device/system-event/>
- Amazfit Balance 2 product notes:
  <https://us.amazfit.com/products/balance-2>
- Amazfit Helio Strap product notes:
  <https://us.amazfit.com/products/helio-strap>
- Amazfit Helio Strap wearing-position FAQ:
  <https://in.amazfit.com/pages/faq/where-is-the-best-place-to-wear-the-helio-strap-for-accuracy>

### 研究发现

WHO 支持 Vitalis 的产品级要求，即计划中应同时保留有氧活动和肌肉强化活动。它支持以每周为单位的背景信息，但不支持准备度评分、精确的训练顺序，也不支持某一天的个人处方。

2026 ACSM position stand 现已成为健康成年人抗阻训练处方最有力的广泛证据来源。它支持每周至少两次渐进式抗阻训练，覆盖主要肌群，并可调整负荷、训练量、动作幅度、爆发意图和每周组数等变量。它还明确为个体化以及参与度/依从性留出了空间。对 Vitalis 而言，这支持通用的动作模式处方和进阶规则，但在用户未提供力量训练历史时，不支持自动给出绝对重量。

并行训练的证据比当前简单的冲突模型更加细致。Wilson et al. 发现，干扰模式受到耐力训练方式、持续时间和频率的影响；在这项较早的分析中，跑步比骑行更容易产生问题。Schumann et al. 总体上没有发现最大力量或全肌肉肥大受到明确损害，但确实发现爆发力发展存在隐患，尤其是有氧和力量训练在同一训练单元中进行时。Vitalis 的“轻松跑加上肢力量训练可以作为附加项，质量跑加下肢力量训练应作为替代项”规则在方向上是站得住脚的，但精确的六小时间隔和 48 小时冲突窗口仍属于产品策略；除非附有更有力的证据，否则应明确如此标注。

RPE/RIR 对调节主观负荷很有用，尤其是在抗阻训练中，但其准确性取决于经验、接近力竭的程度、负荷、练习类型以及用户对量表的熟悉程度。Vitalis 应继续将 RPE/RIR 存储为用户报告的背景信息，而不是将其转换为精确的负荷估计。未来的反馈流程应通过询问“你还能以标准动作完成多少次？”之类的具体问题来教授该量表，而不是假定每位用户都已经理解 RIR。

Zepp OS 证实了本地 Balance 2 桥接的边界：心率 API 提供分钟级历史记录、当前值/最近值、回调、训练状态/历史记录、从 API_LEVEL 4.2 开始提供的用户心率区间设置、系统事件以及 Side Service `fetch`。文档并未确立有保证的连续回调频率。当前桥接说明没有声称固定频率，这是正确的。

Amazfit 当前的产品页面仅支持产品功能层面的陈述：Balance 2 提供 BioTracker 6.0 PPG、睡眠 HRV、训练负荷、恢复时间和可配置的心率监测间隔；Helio Strap 支持佩戴于手腕或上臂、每秒追踪声明、实时心率广播以及训练/恢复指标。这些是制造商声明，并非独立的特定型号验证。本轮研究未发现经过同行评审的 Balance 2 或 Helio Strap 验证研究，因此不应升级 `device_validity.status`，而应继续保持为 `UNKNOWN` 或证据有限状态。

### 产品启示

- 将运动处方规则绑定到已实现的 `ACSM_RESISTANCE_TRAINING_2026`、`CONCURRENT_TRAINING_2022`、`RIR_RPE_SCALE_2016` 和 `RIR_ACCURACY_REVIEW_2026` 证据记录。
- 将精确的同日间隔、48 小时下肢冲突窗口和阈值间歇模板保留为带版本的产品策略，除非未来证据足以支持更具体的声明。
- 让每个计划训练单元上的结构化证据 ID 与规则变更保持同步，而不是仅依赖说明性证据字符串。
- 在使用 RIR 提高计划置信度或规定更高强度之前，添加面向用户的 RIR 熟悉流程。
- 保留当前设备边界：制造商的功能声明可用于标签和设置指导，但不能用于确定特定设备的准确性状态。
- 保持解码后的 `SEC_HR` 流按设备隔离。仅将其数值用于睡眠窗口和运动心率分析；不要将每秒脉率重新解释为逐搏间期，也不要据此推导 HRV。

### Intelligence 7.0 中的实现

- `PlannedSession.evidence_ref_ids` 现在会将跑步和抗阻训练处方绑定到证据库，而不再让来源停留为相互分离的元数据。
- 跑步剂量会使用近期个人训练持续时间、个人阈值心率、近期高强度训练的时间、心率漂移、步频背景以及并行的下肢负荷。
- 在明确的动作、组数、重复次数、负荷、休息以及 RPE/RIR 事实可用时，力量训练剂量会复用它们。心率结构仍然无法识别具体动作。
- 普通分钟级心率仅在已记录的睡眠窗口内进行分析，并设有覆盖率门槛、同设备 28 晚对比，以及独立的同设备 HRV 七天对比。它不用于重建逐搏 HRV。
- 官方 Zepp Android 契约已验证为文件索引 -> 签名 HTTPS ZIP -> `DailySecondHeartBeat` protobuf。连续整数心率值按一秒间隔解码，将 `255` 作为缺失值丢弃；归档条目通过一对一重叠分配映射到设备区间，并在之后的同步中跳过已解码行。夜间资料仅查询实际睡眠窗口，并在分析前按设备聚合为分钟中位数。

## 2026-08-30：多设备 HRV 融合与每日报告

### 已审阅来源

- Task Force HRV measurement standards (1996):
  <https://pubmed.ncbi.nlm.nih.gov/8737210/>
- Schäfer and Vagedes review of PPG pulse-rate variability versus ECG HRV (2013):
  <https://doi.org/10.1016/j.ijcard.2012.03.119>
- Hettiarachchi et al. upper-arm Polar OH1 exercise validation (2019):
  <https://pubmed.ncbi.nlm.nih.gov/31120968/>
- Schweizer and Gilgen-Ammann arm-versus-wrist exercise validation (2025):
  <https://pubmed.ncbi.nlm.nih.gov/40116771/>
- Dial et al. five-device nocturnal RHR/HRV validation (2025):
  <https://pubmed.ncbi.nlm.nih.gov/40834291/>
- Bland and Altman measurement-agreement method (1986):
  <https://doi.org/10.1016/S0140-6736(86)90837-8>
- PubMed searches for `Amazfit Helio Strap` and `Amazfit Balance 2`, completed
  2026-08-30, returned no model-specific validation records.

### 研究发现与策略

运动心率和夜间 HRV 问题需要采用不同的来源策略。在所审阅的运动研究中，上臂 PPG 的表现优于手腕 PPG，但这些结果仅适用于受测的 Polar 型号。它们支持在已解码且归属明确的运动心率数据中优先采用该形态，但并不能独立验证 Helio Strap，也不能证明每秒记录更加准确。

即使在低运动量的睡眠条件下，夜间 HRV 表现在不同受测消费级设备型号之间也存在显著差异。因此，仅凭佩戴位置不足以制定睡眠 HRV 选择规则。Vitalis 应根据可用的 28 天覆盖率、当前观测密度和连续性来选择一个规范的同指标流，并且仅将该数据流与其自身基线进行比较。

不同设备和专有算法尚未证明可以互换。不得对原始 HRV 毫秒值取平均。辅助设备继续作为审计证据和无提示佐证：一致结果不会提高置信度，而在可比条件下方向不一致则可以降低置信度。仅当这种不一致会削弱依赖恢复情况的建议时，报告才应提及它。

每日报告属于呈现层，而非审计信息转储。完整的按设备数据流、多窗口趋势、局限性和规则证据仍保留在 DailyProfile 中。推送应仅选择能够解释或改变今日行动的事实，省略空白/未知部分，并将保留的任何事件转化为实际影响。

## 2026-08-30：日间动态 HRV 解读

### 已审阅来源

- Task Force short-term HRV measurement standards (1996):
  <https://pubmed.ncbi.nlm.nih.gov/8737210/>
- Laborde et al. psychophysiological HRV recommendations (2017):
  <https://doi.org/10.3389/fpsyg.2017.00213>
- Society for Psychophysiological Research publication guidance, including ambulatory
  ECG/PPG measurement (2024): <https://doi.org/10.1111/psyp.14604>
- Sammito et al. review of physiological, disease, lifestyle, and external HRV
  confounders (2024): <https://doi.org/10.3389/fphys.2024.1430458>
- Järvelin-Pasanen et al. occupational stress and HRV systematic review (2018):
  <https://doi.org/10.2486/indhealth.2017-0190>
- Vrijkotte et al. activity- and posture-adjusted ambulatory work-stress study (2000):
  <https://doi.org/10.1161/01.HYP.35.4.880>
- Saygin et al. ambulatory respiratory-control study (2025):
  <https://doi.org/10.1016/j.biopsycho.2025.109171>
- Schneider et al. positive affect and HRV systematic review (2025):
  <https://doi.org/10.1007/s11886-025-02299-4>

### 研究发现与产品边界

动态迷走神经介导的 HRV 可以提供有关生理唤醒的信息，但瞬时 RMSSD 值并不是对压力或情绪的直接测量。职业压力研究通常会结合问卷和较长的观测窗口，发现较高的慢性压力与较低的 RMSSD 或其他副交感神经指标相关。这些证据不能证明可以将可穿戴设备在日间的每个低值都标记为压力。

姿势、身体活动、呼吸、一天中的时段、近期食物/咖啡因/酒精摄入、温度、疾病和测量质量都可能改变 HRV。经过活动和姿势校正的动态研究说明了为何必须将代谢背景与非代谢变化分开。未测量或控制呼吸行为时，它也可能导致虚假的个体内心理关联。

积极情绪与迷走神经介导的 HRV 之间存在取决于背景的关联。所审阅证据发现，特质、静息、激活和瞬时积极情绪的关联方向各不相同。因此，Vitalis 绝不能把某个 HRV 片段解释为“开心”。情绪声明需要生态瞬时评估等时间对齐的主观报告。

真实的 Zepp `HRVRMSSD/real_data` 流包含归属明确的日间样本，但并不连续：样本以每分钟间隔的连续片段出现，片段之间有很长的空档；即使之后进行了同步，当天的数据也可能只上传到上午。缺失区间表示覆盖情况未知，而不是高压力。

### 分析策略

- 将夜间恢复 HRV 和日间动态 HRV 保持为不同契约。
- 保留一个规范的同设备 RMSSD 流，并且仅将 `lnRMSSD` 与该数据流自身的历史记录比较；不要合并不同可穿戴设备的毫秒值。
- 在进行解读之前，先显示能够体现空档的日间覆盖情况描述。不要跨缺失区间连接片段，也不要用零替代。
- 仅聚合采样充分的静止窗口。五分钟 HRV 标准为最低观测概念提供参考，但在选择最终窗口规则之前，必须验证 Zepp 专有的单样本计算窗口。
- 建立按时段划分的个人参考，避免将昼夜节律变化视为压力反应。
- 在生成“较低生理负荷”候选项之前，要求具备时间对齐的低活动量/姿势背景，并排除训练。目前的每日步数和训练摘要不足以满足这一门槛。
- 在获得合适的日间呼吸数据流之前，将呼吸视为明确的置信度限制因素。
- 在学习或报告个人情绪关联之前，要求提供主观心情输入。绝不根据 HRV 单独推断快乐。

描述性第一阶段已经实现：Zepp 的夜间 `sleepHRV`、面向睡眠的 `hrv_sdnn` 和带时间戳的 `HRVRMSSD` 流保持分离。恢复分析优先采用夜间摘要。报告会从基线最强的规范设备流中绘制七个夜晚的 `sleepHRV` 值，不会合并不同设备的绝对 HRV 值。稀疏的 SDNN 仍可在存储中使用，但不会出现在报告趋势中。带时间戳的 RMSSD 仍保留在结构化的每日资料中，但其仅睡眠时段的曲线不会出现在晚间报告中。稀疏的日间 RMSSD 记录仍可在存储中使用，不会被呈现为虚构的全天曲线。分钟级数据不能确立逐搏或 ECG 等价性，任何一个 HRV 流都不会被转换为压力、恢复或情绪声明；依赖背景的日间解读仍会被上述活动、姿势、呼吸和按时段参考要求所阻止。

## 2026-09-02：OpenStrap 与公开的 WHOOP-style Insights

### 证据边界

WHOOP 公开描述了 Recovery、Strain、Sleep 和指导输入、产品范围及高层架构，但没有公开生产环境中的权重、标准化、校准或建议模型。独立研究主要验证底层 HR/HRV/睡眠测量，而不是综合的 Recovery 或 Strain 分数。因此，Vitalis 不会复刻 WHOOP 0–100 Recovery、0–21 Strain、WHOOP Age 或 AI Coach 决策。

OpenStrap 将 `OpenStrap/edge` 中的 Flutter 收集/UI 与 `OpenStrap/analytics` 中采用 MIT 许可的公式分离。Vitalis 固定使用上游提交 `45d72ed989c004008b919b366cd5ceda7061b7df`，且仅移植透明的方法：稳健 EWMA 约定、夜间 lnRMSSD 准备度、稳健多变量异常筛查、睡眠时间/规律性，以及包含 ATL/CTL/TSB 的 Banister TRIMP。`THIRD_PARTY_NOTICES.md` 记录了许可证和修改内容。

### 当前数据审计

当前存储的数据集在重新构建全新架构后可以支持已实现的影子算法：

- 最近 28 天：28 个睡眠日、28 个 RMSSD 日、27 个睡眠 HRV/RHR 日、28 个心率日和 28 个呼吸频率日；
- 42 天负荷窗口：26 次带详细心率数据的训练，以及 31 个设备区间/最大值锚点；
- 用户确认的资料：`sex=MALE`、`HRmax=190 bpm`；
- 不可用的输入：睡眠目标、小睡、Journal 行为、RR/IBI 和加速度计数据。

当前实时数据库仍使用已被取代且不含 `workout_metric_samples.source` 的训练详情架构。不得原地升级。实际启用时，需要另行获批重新构建全新架构并重新摄取数据。

### 产品策略

- Open Health Insights 设置为 `shadow_only=true`，且不会改变 Decision Policy 7.0。
- 用户确认的生理信息优先级高于供应商/设备候选值和训练观测值。
- 夜间 RMSSD 绝不混合不同的来源/设备流，也不以 SDNN/sleepHRV 替代。
- TRIMP 使用一个训练心率流、同日规范 RHR 和已确认的 HRmax。上游覆盖缺失时会产生下限性质的 PARTIAL 结果。
- ATL/CTL/TSB 是描述性负荷，而不是恢复情况、竞技状态、过度训练或受伤风险。
- 在获得小睡数据之前，睡眠目标差值不计入小睡补偿，也不称为生理睡眠债务。
- 多变量异常标记仅表示相对于个人历史的持续偏离，不具备诊断意义。

### 一手来源

- OpenStrap Analytics: <https://github.com/OpenStrap/analytics>
- OpenStrap algorithm catalog: <https://github.com/OpenStrap/analytics/blob/main/ALGORITHMS.md>
- WHOOP Recovery API: <https://developer.whoop.com/docs/developing/user-data/recovery/>
- WHOOP Sleep API: <https://developer.whoop.com/docs/developing/user-data/sleep/>
- WHOOP Strain overview: <https://www.whoop.com/us/en/thelocker/how-does-whoop-strain-work-101/>
- Bellenger et al. wearable HR/HRV validation: <https://pmc.ncbi.nlm.nih.gov/articles/PMC8160717/>
- Miller et al. WHOOP sleep validation: <https://pubmed.ncbi.nlm.nih.gov/32713257/>
- Morton, Fitz-Clarke and Banister load model: <https://pubmed.ncbi.nlm.nih.gov/2246166/>

## 2026-09-02：Personal Health Insight Agents 与 ZeppBridge

### 范围与来源

本次审查评估相关思路和实现边界。它不批准新的健康智能策略、新的数据源或临床使用场景。

- Merrill et al., *Transforming wearable data into personal health insights using
  large language model agents*, Nature Communications 17, 1143 (2026):
  <https://doi.org/10.1038/s41467-025-67922-y>
- Open article: <https://pmc.ncbi.nlm.nih.gov/articles/PMC12855967/>
- Preprint: <https://arxiv.org/abs/2406.06464>
- Released PHIA research code:
  <https://github.com/yahskapar/personal-health-insights-agent>
- ZeppBridge local-first project and reviewed v2.1.0 release:
  <https://github.com/lingcang728/ZeppBridge> and
  <https://github.com/lingcang728/ZeppBridge/releases/tag/v2.1.0>
- Zepp OS app-service guide and API_LEVEL 4.2 release notes:
  <https://docs.zepp.com/docs/guides/framework/device/app-service/> and
  <https://docs.zepp.com/docs/guides/version-info/new-features-42/>

### PHIA 处置结论

该论文中可借鉴的模式包括：针对事实型和开放型健康问题的基准分类法、明确的工具错误恢复机制，以及对来源观测、证据、解读和建议的分离。Vitalis 可以将这些模式复用于以下方面的回归夹具：缺失数据、溯源、弃答、不安全建议、个性化，以及呈现/工具路由。

PHIA 发布的实现会在 ReAct 循环中生成 Pandas 程序，并通过进程内 `exec`/`eval` 执行。它没有可见的进程沙箱、资源预算、文件系统策略或检索提示词注入边界。Vitalis 拒绝采用该实现。确定性的 Intelligence 层仍是唯一获准计算健康状态或训练决策的层。

未来任何语言模型集成都可以读取带类型的 Vitalis 契约，并生成结构化路由或呈现输出。它不得执行生成的代码、计算或补填健康事实、改变规范化记录、覆盖确定性决策，也不得将未版本化的网络材料作为用户特定的证据。PHIA 论文和代码采用 CC BY-NC 4.0；它们是研究参考资料，并非代码、提示词、数据集、测试或实质性表达的来源，除非另行完成权利审查。

### ZeppBridge 对比

| 功能 | Vitalis 状态 | 处置结论 |
| --- | --- | --- |
| 来源/设备溯源和幂等写入 | `(source, scope, device)` 身份加方言原生 upsert 已经存在 | 已实现 |
| 持久化同步重试和重启恢复 | 尝试/分块账本、租约、心跳、重试状态和公平调度已经存在 | 已实现 |
| 历史分页/回填和重放 | 当前端点使用有界分块；游标语义尚未经过夹具验证 | 推迟，等待端点夹具 |
| 缺失的睡眠/压力值 | 缺失数据保持缺失，并阻止缺乏依据的完整性声明 | 已实现 |
| `all_day_stress` 曲线 | 明确的 UTC 数据点时间戳、0-100 数值、空档和设备归属已通过夹具及账户验证 | 作为供应商观测实现；不用于决策 |
| 其他供应商目标或 PAI 区间详情 | 单位、时间基准、设备归属和缺失值语义尚未经过契约测试 | 仅为候选项 |
| `Charge/stress_data` 和 `Charge/insight_data` | 已观测到 protobuf/insight 信封，但内部字段语义仍未记录，且无法映射到已验证的压力 UI | 保持禁用；无健康指标 |
| FIT 导出/导入 | 需要数据导出授权、文件/资源限制、冲突语义、解析器许可证审查和夹具 | 推迟 |
| 心率漂移呈现 | Vitalis 已具备保守且带版本的跑步分析 | 保持当前策略；仅与带版本的夹具比较 |

ZeppBridge 采用 MIT 许可证，其上游声明文件中包含一则源自 Apache-2.0 的训练解码声明。任何保留的或未来新增的、派生自上游的 Vitalis 代码，都必须在 `THIRD_PARTY_NOTICES.md` 中记录经过审阅的上游修订版本、受影响模块、修改摘要和所有必要声明。供应商压力仅作为专有供应商观测保留；它不是心理状态或诊断声明。一次同账户、同设备、按日期匹配的 Zepp UI 对比验证了每日摘要字段的含义和 `all_day_stress.data` 时间线的形态。它没有确立任何 `Charge/stress_data` protobuf 字段或 `Charge/insight_data` insight 类型的含义。

### Balance 2 桥接启示

API_LEVEL 4.2 提供手表侧心率区间设置和设备 UUID。当前官方 app-service 契约使用 `file` 启动属性。Balance 2 桥接必须在硬件上验证该契约，在回调样本可能因服务重启而丢失之前将其持久化，公开有界队列的逐出情况，并保留幂等重试行为。在模拟器或 Developer Bridge 中成功，并不足以证明后台服务在实体设备上的可靠性。
