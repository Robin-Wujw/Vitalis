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

## 真实配对

真实模式要求新的独立数据库、`ZEPP_MOCK=false`、有效 `VITALIS_TOKEN_ENCRYPTION_KEY` 和必要应用配置。先运行 `vitalis db init` 和 `vitalis user create --id <用户>`，用 `manage` scope 令牌创建 `POST /api/connect/zepp/pair` 会话，再打开返回的 `scan_url`。账号密码和验证码只在官方 `watchface.zepp.com` 或 `user.huami.com` 页面输入。

浏览器扩展中输入服务源地址和一次性配对码；非 localhost 地址必须为浏览器信任的 HTTPS。服务端的 `VITALIS_PAIRING_ALLOWED_ORIGINS` 要包含实际的 `chrome-extension://<扩展 ID>`，重启 API 后再配对。详细浏览器操作见[扩展指南](../clients/browser_extension/README.md)。配对码、浏览器链接令牌和厂商令牌用途不同，不能互换。

网页登录完成后，当前适配器使用官方页面产生的 `apptoken`、源用户 ID 和允许的区域主机。区域主机必须是 HTTPS 的 `api-mifit*.zepp.com` 或 `api-mifit*.huami.com`，不接受凭据、路径、查询或任意端口。常见中国区域是 `api-mifitcn.zepp.com`；实际区域按账号返回值和配置决定，不要手工猜测其它用户区域。

### 网页登录

不用插件、在电脑浏览器里用 Zepp 手机号（或邮箱）和密码连接：`vitalis connect zepp --user <用户>` 生成配对会话的 `login_url`（`/api/connect/zepp/login?code=…`，默认 60 分钟有效），在连着 ZeroTier 的电脑上打开即可；`--push` 另经 PushPlus 发一份。页面左侧说明数据去向和进度（登录 → 加密保存令牌 → 回填 180 天 → 每天同步），右侧是登录表单；密码错误、地区不对、要图形验证、尝试过多等情况各有一句提示（API 只经 `X-Login-Failure` 头给出固定分类码，不转发 Zepp 原文）。Zepp 官方网页的手机验证码只用于注册和重置密码，不能直接登录（2026-10-14 核对其当前脚本）；没有密码时页面引导到 Zepp 官方开放平台 `console.zepp.com`，它按账号地区转到官方登录页，在那里用“忘记密码”和手机验证码设置密码（`user.zepp.com` 不是在所有网络都能打开）。“密码只经过自己的 Vitalis 服务器一次、不保存”的方案已于 2026-10-13 经用户确认；生产 API 只在本机监听，经 ZeroTier 私有网络访问（2026-10-14 用户决定），入口配置见[运维](operations.md)；真实账号走通前保留浏览器插件，见[改动计划](plan/zepp-web-login.md)。

登录页按 Zepp `user.zepp.com/universalLogin` 当前生产 JavaScript 使用的两步表单流程：先向 `api-user.huami.com/registrations/{account}/tokens` 换取 `access`，再向 `account.huami.com/v2/client/login` 换取 `app_token`、`login_token` 和 `user_id`。成功必须同时满足第二步 `result=ok` 和三项令牌字段存在；任何未知响应都不会被当作成功。这个 legacy 网页协议不是 Zepp 官方 OAuth API，实际部署仍需目标环境单独验收。

`app_token` 保存为加密 access token，`login_token` 保存为加密 `AuthToken.refresh_token`。同步返回需要重新认证时，worker 用 `GET account.huami.com/v1/client/app_tokens?login_token=…` 换新的 `app_token`（Zepp 应用使用的接口，不在公开文档里，尚待真实账号验证）；响应缺令牌或属于别的账号时视为失败，状态保持 `needs_reauth` 并发重新登录链接，不伪造刷新成功。验证码请求被拒绝，页面只引导到 Zepp 官方重置页。

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

训练明细先落盘再流式解析，单条响应最多 32 MB、展开后最多 25 万个样本（各逐秒序列合计约每秒 10 个，约 6.5 小时）；超过时记为 `resource_limit`，不记成无法识别。实测最大的真实明细是 4 小时 39 分的马拉松：响应 1.6 MB、16.7 万个样本，服务器上解析 4.1 秒、进程峰值 309 MB，在 worker 的 500 MB 软上限内。2026-10-14 以前的上限是 2 万，普通 35 分钟的跑步就会超出，9/28 以来缺失的 71 条明细已用 `vitalis sync details` 补齐。

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

## 协议和安全边界

当前代码按 ZeppBridge 公开行为适配区域端点、认证头和部分 payload，但 Vitalis 的来源/设备隔离、同步租约、覆盖状态和确定性分析是本项目边界。未知字段不自动变成已知指标，协议事实需要测试和第三方来源记录；相关第三方原文见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

不要把 APK、Cookie、apptoken、密码、验证码、真实响应或个人记录加入仓库。凭据由服务端加密保存，密钥与数据库分开；日志只保留非秘密分类。真实 Zepp 接入仍需在目标环境单独验收，mock 通过不能替代线上授权验证。
