# Zepp 连接与同步

[文档中心](README.md) | [快速开始](quickstart.md) | [数据合同](data-contracts.md) | [扩展指南](../clients/browser_extension/README.md)

Vitalis 当前支持 Zepp 作为外部数据源。dev/test 环境默认使用离线 mock；prod/production 环境只允许真实连接，使用官方网页登录会话和区域云端接口。配置规则见[运维](operations.md)。mock 验收不等于真实账户或真实 Hermes/PushPlus 联调。

## Mock 路径

从仓库根目录、使用新 SQLite 路径运行：

```powershell
$env:ZEPP_MOCK = 'true'
$env:VITALIS_ENV = 'test'
$env:DATABASE_URL = 'sqlite:///./demo.db'
uv run --locked --extra dev vitalis demo --database .\demo.db --day 2026-10-07
```

Mock 生成同构的睡眠、活动、训练和来源元信息，可离线执行分析和报告读取。它不需要 Zepp 凭据，也不应与真实用户库混用。同步、分析和报告仍通过各自的持久任务/快照边界运行。

## 真实连接

不要把 mock 演示库改成真实库。首次建立真实环境时，使用新的独立数据库，设置 `ZEPP_MOCK=false`、有效的 `VITALIS_TOKEN_ENCRYPTION_KEY` 和必要应用配置，再运行 `vitalis db init`、`vitalis user create --id <用户>`。已有真实环境继续使用现库。

### 网页登录（主入口）

不用插件，在电脑浏览器里用 Zepp 手机号或邮箱和密码连接：

1. 按[运维说明](operations.md#zepp-网页登录与重新登录)配置可访问的服务地址。当前私有部署经 ZeroTier 访问；公网入口必须使用 HTTPS。
2. 使用绑定本人且有 `manage` scope 的 Bearer，通过 `vitalis api request POST connect/zepp/pair`（stdin 输入 `{}`）取得并打开 `login_url`；默认 60 分钟有效。
3. 在用户自己的 Vitalis 登录页输入账号和密码。密码只经该服务转交 Zepp，不保存；成功后加密保存令牌，并启动历史回填。
4. 查看连接进度，确认回填和首次分析完成。登录成功不等于历史数据已经齐全。

页面说明“登录 → 加密保存令牌 → 回填 180 天 → 每天同步”的进度。密码错误、地区不对、需要图形验证或尝试过多时，显示固定分类提示，不转发 Zepp 原始响应。不要在聊天、命令参数或日志里传密码、配对码和令牌。

按 2026-10-14 核对的官方网页流程，手机验证码用于注册或重置密码，不能直接登录。没有密码时，按页面指引到 `console.zepp.com`，由官方按地区跳转，再用“忘记密码”设置密码；不是把验证码交给 Vitalis。

### 令牌保存与续期

登录使用两步网页协议：先向 `api-user.huami.com/registrations/{account}/tokens` 换取 `access`，再向 `account.huami.com/v2/client/login` 换取 `app_token`、`login_token` 和 `user_id`。第二步必须同时满足 `result=ok` 和三个字段齐全；未知响应不算成功。这不是公开的 Zepp OAuth API，需要在目标环境单独验证。

`app_token` 和 `login_token` 都加密保存，后者进入 `AuthToken.refresh_token`。同步要求重新认证时，worker 尝试用保存的 `login_token` 换新令牌；响应缺令牌或属于另一个账号时保持 `needs_reauth`，再发重新登录链接。这个续期接口尚待真实账号验证，不能因代码存在就宣称可用；剩余验证见[网页登录计划](plan/zepp-web-login.md)。

### 浏览器插件配对（暂留备用）

这条旧路径和上面的网页登录不同：用 `manage` scope 令牌创建 `POST /api/connect/zepp/pair` 会话，打开返回的 `scan_url`；账号密码和验证码只输入官方 `watchface.zepp.com` 或 `user.huami.com` 页面。

在插件中填写服务源地址和一次性配对码；非 localhost 地址必须为浏览器信任的 HTTPS。`VITALIS_PAIRING_ALLOWED_ORIGINS` 要包含实际的 `chrome-extension://<扩展 ID>`，重启 API 后配对，详见[扩展指南](../clients/browser_extension/README.md)。配对码、浏览器链接令牌和厂商令牌用途不同，不能互换。

旧插件仍在仓库中。完成约定的回填、日常报告和稳定性验收后再安排退役，不因已经成功登录一次或到了预定日期就自动删除，见[清理计划](plan/zepp-web-login.md)。

### 区域主机

连接后使用账号返回的厂商令牌、源用户 ID 和允许的区域主机。主机必须是 HTTPS 的 `api-mifit*.zepp.com` 或 `api-mifit*.huami.com`，不接受凭据、路径、查询或任意端口。常见中国区域是 `api-mifitcn.zepp.com`；以账号返回值和配置为准，不猜测其它用户的区域。

## 同步与任务

API 只创建同步任务；独立 `vitalis worker` 执行厂商请求、分块、租约、重试和覆盖账本。创建任务时带 `Idempotency-Key`，可用明确本地日期 `from`/`to` 或 1–730 天窗口：

```text
POST /api/sync-jobs
Authorization: Bearer <sync-token>
Idempotency-Key: <new-or-reused-key>
{"from":"2026-10-01","to":"2026-10-07","source":"zepp"}
```

响应中的 `job_id` 只表示已入队。用 `GET /api/jobs/{job_id}` 查看 attempt/chunk 状态，用 `GET /api/data-status` 查看覆盖和最近结果。取消需要 `sync` scope 的 `POST /api/jobs/{job_id}/cancel`。来源撤销使用 `manage` scope 的 `POST /api/sources/zepp/revoke`；撤销保留本地事实，但 fencing 旧 worker，不能把旧凭据状态写回。

首次连接成功后，worker 回填 180 天历史，完成后自动排队首次分析。进度用 `GET /api/connect/zepp/progress` 读取，状态依次为 `connected` → `credential_verified` → `backfill_requested` → `backfill_progress` → `bootstrap_analysis_queued` → `first_report_ready` → `daily_cadence`，同时给出实际覆盖和暖机进度。

训练明细先落盘再流式解析，单条响应最多 32 MB、展开后最多 25 万个样本（各逐秒序列合计约每秒 10 个，约 6.5 小时）；超过时记为 `resource_limit`，不记成无法识别。实测最大的真实明细是 4 小时 39 分的马拉松：响应 1.6 MB、16.7 万个样本，服务器上解析 4.1 秒、进程峰值 309 MB，在 worker 的 500 MB 软上限内。明细回填统一通过 `POST /api/sync-jobs` 的 `detail_only`/`detail_limit` 参数排队，每批成功后核对覆盖再继续。

## 补充数据与可取性

查询基准为 [zepp-mcp 固定提交](https://github.com/DhavalBhimani44/zepp-mcp/tree/904cc301d63a64da6f633dd8a75b2a612e67240a) 和 [ZeppBridge](https://github.com/lingcang728/ZeppBridge/tree/a6a805dce7fba941af544e36d251c6d6026a1a21)。2026-11-10 已从授权服务器核查睡眠时间组件、REM、训练毫秒时长、高精度距离、DailyHealth 目标、本人历史体重与 exertion 厂商负荷；这不代表所有数据都出现在本人账号。体重 endpoint 使用 `members/-1/weightRecords`，fromTime/toTime 为秒；返回仍按 memberId 再过滤。当前同步计划 `zepp-sync-v9`、解析器 `zepp-parser-v6`；请求目录由 `streams.py` 同时供单次取数和持久任务使用。

Charge stress/insight 和 readiness watch_score_data 有非空原始内容，但编码语义尚未验证，保留加密原文；readiness watch_score 的可解释字段单独标准化。三字节分钟活动仅在 1,440 槽位、时区、整日及活动时段总量同时核对后开放步数，kind/intensity 字节不命名为活动类型；八字节或对账失败的形状保持 unverified。本人分钟步数仍需实际对账，不能用合成通过代替。Food、Emotion、血压、恢复心率等可通过有界原始 GET 探查，账号是否有记录、厂商是否支持与原文能否解码分别处理。AFib 的 255 等占位值继续排除。

### 与 14 个 MCP 工具的映射

`GET /api/sources/zepp/schema` 是机器可读的完整目录，包含 14 个工具映射、运动代码、流编码/单位、圈段列、字段资格和已知限制；参数以生成的 [API 参考](../skills/vitalis/references/api.md)为准。

| MCP 工具 | Vitalis HTTP 查询（省略 `/api`） |
| --- | --- |
| daily_summary | `/health/daily-summary`，日期范围、各来源观测日总量、小时/分钟步数、原始活动模式码 |
| heart_rate | `/health/heart-rate`，指定日、均值/极值、分钟覆盖和保留空槽的分钟序列 |
| sleep | `/health/sleep`，主睡眠、午睡、阶段、在床时间、睡眠窗呼吸/HRV，以及保留独立来源的同日报告日期 readiness/ODI；同日关联不冒充睡眠窗采样 |
| list_workouts | `/workouts`，运动/类型/家族/来源过滤，limit/offset 分页 |
| workout_detail | `/workouts/{id}/sections`，summary/laps/splits/streams/gps 显式选读 |
| body_composition | `/health/body-composition`，本人记录、BMI一致性及所有可选体成分；`limit`/`before` 与游标分页 |
| training_thresholds | `/health/training-thresholds`，事件历史与独立训练估值、VO₂max/负荷历史 |
| health_metrics | `/health/summary/{category}`，9 类健康指标、日统计、趋势与可选样本 |
| workout_stats | `/workouts/statistics`，任意范围、周/月/年/运动分组、20 类趋势与范围纪录 |
| period_summary | `/health/period-summary`，活动/睡眠/运动/恢复独立状态与覆盖 |
| profile | `/sources/zepp/profile`，Zepp 身体档案及允许公开的设备/固件字段 |
| describe_schema | `/sources/zepp/schema` |
| raw_request | `POST /sources/zepp/raw-requests` 后读取返回的 result_url |
| auth_status | `/health/token-status`，凭据元数据与最近验证状态，不返回令牌 |

同步新增 single_stress、PAI 全部分区及最新上传版本、呼吸分钟、体成分、profile 和设备元信息。体成分 community_schema 不等于本人设备验证；muscleRate 与 oneFootMeasureTime 不猜单位，BMI 不一致时保留 vendor_bmi。游泳 SWOLF、泳姿/长度、跳绳、跑姿、厂商计划进度和 PB 单独保留来源资格。区间最快值是该区间纪录，不冒充终身 PB；不同运动的配速不平均。

日汇总的 `summary.指标[].summary.total` 提供步数、距离、热量和活动分钟的观测日总量；周期摘要复用 `activity.summary`。各来源、设备、单位、资格独立，缺日不补零，同日多候选总量置空并标记 `ambiguous_daily_records`。体成分默认读取最新记录，`before` 包含该本地日期；`from`/`to` 可进一步限定日期范围。沿返回的 `next_cursor` 翻页，保证同一时刻的不同测量不漏、不拆开指标；读完分页仍不代表厂商历史同步完整。

上述健康摘要接受任意可转换为服务时区 UTC 窗口的日期范围，不再按 730 天拒绝历史查询；整体趋势由服务端对完整请求窗口、各来源分别计算。单请求的实际读取预算为 50,000 条紧凑记录、50,000 个指标来源日单元、2,000,000 次流式样本读取、16 MiB 紧凑输入和 32 MiB JSON 结果；周期摘要的健康部分共用读取预算，重复读取也计入工作量，训练部分另受 `WorkoutStatistics` 的 10,000 场上限约束并标记 `truncated`，整份结果仍共用 32 MiB 输出预算。超限返回稳定 HTTP 413，不能把截断结果说成完整统计；缩小窗口后取得的局部趋势也不代表原窗口整体趋势。分钟活动仍最多 31 天，小时和分钟数组仅显式请求时读取，活动阶段及资格始终保留。SQL 先选截止时刻的最新来源日版本，随后投影并分批读取，长期密集样本保持流式处理。

睡眠 `summary` 的睡眠/阶段/在床分钟提供各来源观测日总量，睡眠评分和效率不累加；延迟与相同来源、设备、睡眠日的静息心率另给均值。厂商直接给出的入睡延迟不依赖在床时间交叉验证，无法对账的在床时间仍未知。阈值 `event_changes` 与 `workout_changes` 分开给同资格的首尾观测差值、日期、样本和覆盖；少于两个观测日或端点多候选时变化未知，`current` 保留最新日全部候选。

日活动 `activity_modes` 按原始模式码和来源累计报告阶段的分钟、步数与已知距离，部分距离缺失只计已观测阶段，重叠阶段不产生确定总量；不猜模式名称。Charge 各来源日给 `first`/`last` 和观测时间，同一端点时刻值冲突则未知；`daily_max_summary`/`daily_max_trend` 描述每日峰值，原均值趋势独立保留，均不是 Vitalis 恢复许可。ODI 的 `spo2_odi_validity_code` 仅在 `days[].observations` 原样展示，不解释有效性、不算均值或趋势。

训练 `series.family_partitions` 按来源与已知运动家族计算组合趋势，组合配速保留距离/时间分母；`best` 是参考指标方向的区间极值及对应场次，不表示身体更健康。厂商 PB 键的未知意义仍保持原始资格。`token_expires_at` 只读已保存到期元数据，可为 null；`country_code` 未保存到公开凭据元数据，不能根据区域主机推断，需专门授权的原始查询才可核对厂商实际返回内容。

合成资源验收使用 1,240 天、2 个来源、每来源日 2 个历史版本及 1,440 分钟数组：4,960 条存储 JSON 共约 84.3 MiB，日摘要返回最新的 2,480 条、JSON 约 1.64 MiB；开启 `tracemalloc` 时用时 1.35 秒，Python 分配峰值约 10.2 MiB。此结果验证长窗口的投影与有界读取，不代表真实服务器延迟或 SQLite 原生内存峰值。

标准查询需要 read scope，读取本地保存值。数据缺失返回 no_data，功能存在不保证账号有数据；统计窗口和覆盖只承诺已保存记录。原始请求与原文读取另需 source_raw：固定已认证区域主机、仅 GET、本人路径用 `users/me`，拒绝主机/身份/凭证覆盖，展开 JSON 最多 2 MiB。任务使用现有 SyncAttempt、幂等键、worker 租约与账号 fencing，HTTP 路由不直接联网。原文加密保存，输出递归移除凭据字段；超预算413、失败502、待完成202。`/raw-records` 先列元信息，再按ID读取；opaque 流保持 raw_unverified。Hermes 当前令牌没有 source_raw。

2026-11-11 服务器通过 HTTP 完成 14 项能力读回：定向健康/档案同步、训练详情刷新和有界原始 GET 均成功。本人账号可读到 PAI 总值/每日/区间分项、呼吸、readiness、档案和设备元信息；体成分当前只有体重、身高与 BMI，不能推断有体脂或肌肉记录。训练详情已按 5.2 合同读回；本人分钟活动形状仍是 `unverified_layout`，不输出伪造的小时或分钟步数。无本人记录的字段通过合成有数据/无数据测试核对实现，不能把这些测试写成真实设备验证。验收用临时维护令牌已撤销，未向真实渠道发送测试通知。

同版 APK 的公开资源 `goal_fatbure_daily` / `goal_set_fatbure_tip` 将目标称为“每日燃脂时间目标”，说明它与燃脂心率区间内的时间有关，不能当全部活动或训练总时长。DailyHealth 的 `totalBurningDuration` 现独立保存为 `vendor_burning_minutes`，与 `burningDurationGoal` 对照；新解析保留实际匹配的源字段。旧日级 `active_minutes` 若没有明确 `activeMinutes` 来源，报告分析暂不采用，原文和历史记录保留供审计；不得把只含 DailyHealth 的回放直接应用到真实用户的整个日期窗口。剩余历史修正边界见[来源修复计划](plan/daily-metric-provenance-repair.md)。

负荷定义的一手证据为同版 APK 的 `phn_exertion_atl_and_ctl_text`、`phn_exertion_training_status_desc` 和 `phn_training_status_formula`：ATL 表示近 7 天活动量加权平均，CTL 表示近 42 天加权平均，TSB 为 CTL−ATL。App 另有运动记录/全天活动统计选项，但 `_total` 与两选项的具体字段映射及加权参数未核实。可以解释名词，不能据此为个人诊断疲劳或许可加量；普通报告省略六字段列表，按需查询仍保留原值和来源。

`GET /api/health/catalog` 列出字段单位、来源、资格与 raw-only 流；`GET /api/health/sleep-records` 读取已保存的独立来源睡眠，`GET /api/health/metrics/{metric}` 和 `/api/health/daily-metrics` 读取样本/日事实。同步由 `POST /api/sync-jobs` 排队，完成状态与覆盖分别查询 `/api/jobs/{id}` 和 `/api/data-status`，读取本身不访问 Zepp。

手动同步可用 `streams` 精确选择请求流，例如 `{"days":60,"streams":["sleep","workouts","wellness/weight","wellness/exertion","wellness/charge_stress","wellness/charge_insights","wellness/readiness_raw"],"decode_dense_files":false}`。名称取自数据状态的 stream 或目录的 stream 字段；空、未知流、与 workout_only/detail_only 等专用模式混用均拒绝。顺序不影响幂等身份。所选 workout 摘要不会顺带触发未请求的明细回填；明细使用独立 detail_only 操作。未提供 streams 仍按完整同步计划执行。

力量 `lap_62` 的普通解析和流式解析共用同一行解析器及完整已核验动作表（`adapters/zepp/data/strength_exercises.json`）。Zepp 10.8.7-play APK 证明 `lap_62[28] → strengthTrainType → actionType`，官方 `/sport/config/muscle` 中文目录共 1,134 项，其中代码 0 是“选择一项训练”界面占位，其余 **1,133 个唯一动作代码全部映射**；原来只收录实际观测的 26 项不再作为覆盖上限。目录响应 SHA-256 为 `7e6b4ba617bd8e61b0c269c9c51fb4d7b430c0e30332273a964fa88f8028abeb`，资源保留 APK 摘要、版本、字段关系和逐项目录定位，不伪造个人观测证据。

目录同时保留 `mainPositions/subPositions` 的原始人体图编号，并为 36 个已审阅条目登记规范动作身份；其余保持未知。2026-10-21 经现有连接取得中文配置，序列化响应摘要仍为 `7e6b4ba617bd8e61b0c269c9c51fb4d7b430c0e30332273a964fa88f8028abeb`。`StrengthTrainingUtils.s → StrengthTrainingAction → STBodyPositionView.c` 确认这些数组用于绘制主要/次要受力部位，共 46 个图层；八类菜单的 `levelId` 是另一命名空间，不能混用。目录条目 42–58 明确给出胸、腹、上臂前后侧等部位及图层，分析只解码有这些文字依据的区域，未明确区域保持未知。原始数组中的重复编号保留作审计，汇总时同一部位每组只计一次。

动作名称仍保留 `exercise_name_reference_mapping` 来源。规范身份、厂商部位参考、用户确认和动作处方资格独立；增加映射不会自动声称用户已确认。没有代码、目录外代码或暂定条目保持未知。普通和流式解析均经过完整目录，再验证落库路径；已有记录分析时使用带版本的目录，原始组次不改写。

同版 APK 的 `my91` 解析与 `p6c0.u` 序列化确认 `lap_62[21]` 是重量原值、`[22]` 是次数、`[23]` 是单位编码；`tc20.c/h/j` 确认编码 `0` 为 kg、`1` 为 lb，`11` 为 kg 且原值除以 10、`12` 为 lb 且原值除以 10。先缩放再检查重量范围，规范记录保留 `vendor_weight_value`、`vendor_weight_unit` 供审计，`weight_value/weight_unit` 为解码后的原单位，明确 lb 按 `0.45359237` 换算 `weight_kg`。`13/14` 是 RM/RPE，不能作为物理重量；未知编码不默认为 kg。

`tc20.g` 的自重显示条件是：有限原值不大于 0，并且单位编码为 `15` 或官方动作的 `selfWeightType=1`。目录保留这项厂商标记，暂定条目不能据此判定自重。符合条件时记为 `weight_basis=bodyweight`，报告显示“自重”，不推断身体质量或容量；正重量仍作为记录的负重显示。不符合条件的零值不改成自重；缺失、非法数值与负值哨兵不补成零。没有单位与口径依据的重量仍不进入容量与 1RM。新增字段不改变明细版本或数据库表结构，已存力量记录通过[运维刷新](operations.md)从加密原始记录重读这几列。

运动大类使用 `adapters/zepp/data/sport_modes.json`：对齐 APK 的 `config_sport_type_group.json`、`device_sport_categories.json` 和 `sport_categories.json`，覆盖 **208 个真实项目**，`-1/Training` 占位独立保存但不解析为运动。189 项有 APK 中文，19 项仅有 APK 英文时保留原名称；原有已核验展示名和分类继续使用，84 个新增且没有分类证据的项目归入 `other`、训练家族 `unknown`，不从名称推断健康事实。

设备 ID 与云端类型是两个命名空间：例如“自由训练”设备 ID **5** 对应云端 **16**，公开水域游泳设备 7 对应云端 15，户外徒步设备 15 对应云端 22。`/run` 历史入口按云端目录解析，旧分运动入口按设备目录解析，禁止跨目录回退。标准化记录保留 `vendor_type_id` 和 `vendor_type_namespace`，即使代码未知也保存来源命名空间。

### 运动后主观用力（RPE）

运动摘要的 `data.summary[].rpe` 保存为 `Workout.session_rpe`。Zepp 10.8.7-play APK 中 `HistoryRecord.rpe`、`Trackrecord.rpe` 对应运动记录，`SportSummary2.OtherMessage.manualRPE_` 和详情页的 `UpdateSportRecordUseCase.UpdateRequest.rpe` 保留手动评分路径；`moving_ui_rpe_rate_your_effort`、`moving_ui_your_rpe` 与 `moving_ui_rpe_rate_1` 至 `_10` 是对应界面资源。此项是 Zepp 记录的用户主观用力，来源资格为 `zepp / workout_summary / rpe / user_reported`，不能改称厂商自动训练评分。

只接受原字段中有限、非布尔的 1–10 数值；缺失、空值、字符串、越界及 `0/-1/255` 都保留未知，不补零，也不尝试解析其它同名评分。规范字段保留 `score_1_10` 单位；`observed_at` 仅关联活动起点，`time_basis=activity_day`，不表示用户填写评分的时刻。`fetched_at` 与 `source_record_id` 来自同用户、同来源的运动摘要原始记录；没有该依据时保持未知，不用当前时间代填。同值、同来源、同活动时间的重复同步保留首次获取依据，评分变化会触发受影响范围重算。来源评分通过训练 JSON 保存，不另建用户反馈、不更改数据库 schema。

### 更新目录

APK、原始响应和个人健康数据不进入仓库；只提交最小规范化映射及来源摘要。用仓库外的原始证据运行生成器：

```bash
uv run --locked --extra dev python tools/extract_zepp_sport_catalog.py --assets /secure/zepp/assets --apk /secure/zepp/base.apk --app-version 10.8.7-play --output src/vitalis/adapters/zepp/data/sport_modes.json --check
uv run --locked --extra dev python tools/extract_zepp_strength_catalog.py --catalog /secure/zepp/muscle-zh_CN.json --apkm /secure/zepp/app.apkm --output src/vitalis/adapters/zepp/data/strength_exercises.json --check
```

首次生成时去掉 `--check`；运动生成器会逐字节验证解包资源与传入 APK 的 ZIP 成员一致。新 APK 或官方响应摘要变化时，先复核字段关系、重复编号、占位、语言及全量覆盖，再更新力量生成器允许的已审查摘要。目录、解析代码均计入分析规则摘要；更新不会原地改写旧分析快照。已存记录的安全刷新和报告重算见[运维](operations.md)。

同步状态区分成功、部分覆盖、需要重新认证、失败和未知；不能把一个流失败或没有密集文件理解成全库成功。迟到数据进入后续分析修订，缺失继续保持缺失。

HRV 请求使用包含首尾两天的本地日期窗口，再转换成 UTC 毫秒范围；同步 attempt 保存的时区会在 worker 恢复时继续使用，不从当前进程环境重新推断。持久同步保留原始事件和逐样本观测时间，mock/demo 路径只生成按本地日聚合的合成样本；两者都按报告时区归属 `calendar_day`。未知设备范围保持未知，不能把 mock 或厂商融合值描述成设备级观测。

## 曲线与新增字段

按 [zepp-dashboard 的公开协议记录](https://github.com/J-solan/zepp-dashboard/blob/main/docs/zepp-api.md)补充午睡、厂商七日负荷区间、自动识别标记、训练标题、训练效果和区间秒数；实现为 Vitalis 的独立类型化解析，没有复制其前端或 Hevy 配对规则。该参考只在单一 Helio Strap/欧区账号验证，不能据此声称本项目全部设备区域已实际验收。

- `slp.odd_stage` 单独解析午睡；正常主睡仍走已有来源和完成资格。
- `band_data.data_hr` 分钟心率与主睡共用同步任务冻结时区；显式时区优先于 payload 固定偏移，无显式时区时仍保留可用原偏移。按本地分钟槽转换 UTC，避免缺少 tz 时整段被当成 UTC 或夏令时后仍沿用午夜偏移；同响应水位只使用本响应有效样本。
- `Charge.samples.total/physical/mental` 分别验证；总分255不影响其余有效分项。
- `readiness.value.timestamp` 按同步任务冻结时区归日，`item.timestamp` 选择最新修订，并向存储传递撤回标记。
- SPORT_LOAD 日负荷与厂商七日累计/区间拆开；旧已保存混口径值需私有回放验证后刷新，不能仅更新排版就宣称历史已经修正。
- 运动摘要的 `auto_recognition` 只接受明确布尔，`sport_title` 保存用户厂商标题；`te` 的0–50编码按公开协议除以10，保留为设备估计，不是Vitalis训练评分。`heart_range` 保存秒数和区间上界，不自动提升为个人阈值模型。

新图表只从已保存规范数据生成，范围与使用渠道见[报告与渠道](reports.md#曲线图与图表-api)。新数据入口仍需真实设备与 App 对照，未接触真实账号的合成测试不代表云端可用性已验证。

## 多设备来源核对

对照 ZeppBridge `1012a00abbb41865e8abcfb8f59b3d264fbafe1b` 的设备normalizer与identity存储，按明确ID、同一目录别名、账户端点语义分别处理，不从无ID或最长逗号片段猜设备。`device_type` 是既有band请求参数，不是已验证的任意deviceId过滤入口；上游常规抓取同样使用固定 detail/8/0，不能据此承诺每项都能单独请求两台设备。

2026-11-03 的只读响应与 Zepp 10.8.7-play APK 核对，**撤回此前“带 Balance 2 标识的心率与压力整条流都属于该设备”的判断**。设备目录中的 MAC、设备 ID、序列号关联仍有效，但响应头的贡献设备不等于每个样本的测量设备；两台设备的 HRV 原始流仍独立保留，Charge 的已核实 App 记账来源仍归账户。

### App 最终结果与贡献设备

本轮 APK 证据对应 base APK SHA-256 `322c37d71a525db026975a60af7852c063218804a5b3258e91299499122acbcf`：

- `MergeStrategy.r` 调用 `p/J` 后写顶层 `summary.isMerged`，该标志说明 `data_hr` 心率与活动汇总已走 App 融合路径。睡眠必须独立检查 `summary.slp.isMerged`；`ss71/ut61/e471` 的睡眠写入与展示链支持这一独立标志，不能用顶层标志替代睡眠标志。
- 已核实的 merged 结果保存为 `source_scope=user_fused`、`device_id=null`；原响应仍带设备标识时另存 `contributing_device_id`，不把账户结果重新分配给这台设备。不自行复刻未披露的融合算法。
- readiness 的 App 记录须精确匹配 `eventType=readiness`、`subType=watch_score`、`deviceId=app` 与整数 `deviceSource=2`；若有 `deviceType`，也必须为整数 `2`，此时归 `user_summary`。APK 的 `qnx0` App 分支构造该身份，经 `bey0.h` 的 `ReadinessGroup`、`wqx0.b` 写出；写出字段不含 `deviceType`，`bey0.d` 明确检查 App 身份。这是特定记录形状的证据，不是“无设备 ID”或任意 `app` 字符串均可提升为账户结果。
- `all_day_stress` 来自 App 的 `PressureRecord` 本地处理：按时槽、主设备优先级择取后，按 UTC 日上传；逐点只保留 `value/time`，日头取首条记录的来源。它是 `user_summary`、`qualification=vendor_selected`，原日头设备保存在 `contributing_device_id`，不能用该日头恢复整日逐点设备归属。被择除的单设备压力不能从此响应重建；尚未证实其它原始压力接口，不据此宣称设备不支持压力。

### 原始设备数据的采集缺口

已确认旧 `SEC_HR` 链路存在“索引已保存、下载任务未创建”的缺口，不是云端没有设备数据。本地候选修正完整同步的默认解码策略，并为窗口内全部相关归档建立持久分片，不再只排一个文件；显式仅取元数据时保持部分覆盖，不能以索引成功代表样本已取得。`fileInfo` 单次返回达到 2,000 项时仍标记部分覆盖；完整分页尚待补齐。

本轮还通过已存在的 `/users/me/fileInfo/events`、`active_origin_data/real_data` 路径核对 `ACTIVE_ORIGIN_DATA` 归档。PB 内容包含设备 SN、设备来源码、日期及每分钟八字节记录；候选解码只保留已证实的心率与原始活动/睡眠模式字段，不将它们直接解释成最终睡眠分期或分数。最终单设备睡眠仍依赖厂商 `nativeAppDataProcessor`，当前不能冒充其输出。空 SN、`deviceSource=-1` 的虚拟条目保持 `unknown`，不因“第三条记录”提升为 App 融合结果。

已完成私密本地心率预览核对：Balance 2、Helio Strap 原始心率及 band 明确 merged 的 App 心率可独立展示；未知虚拟来源另列。这仅证明本轮心率三路可取得与区分，不代表全部指标齐全或用户已完成 App 对照验收。2026-11-03 经明确授权完成服务器切换、六十天原始来源刷新和最近三天归档补采，今日日报经保存及 API 读回核对；本次维护与预览未额外发送通知。没有来源证明的旧设备行保守保留，不能声称所有历史标签已消除歧义；剩余工作见[图表验证计划](plan/report-chart-validation.md)，三路显示规则见[报告与渠道](reports.md#设备与账户汇总分开查看)。

## 协议和安全边界

当前代码按 ZeppBridge 公开行为适配区域端点、认证头和部分 payload，但 Vitalis 的来源/设备隔离、同步租约、覆盖状态和确定性分析是本项目边界。未知字段不自动变成已知指标，协议事实需要测试和第三方来源记录；相关第三方原文见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

不要把 APK、Cookie、apptoken、密码、验证码、真实响应或个人记录加入仓库。凭据由服务端加密保存，密钥与数据库分开；日志只保留非秘密分类。真实 Zepp 接入仍需在目标环境单独验收，mock 通过不能替代线上授权验证。
