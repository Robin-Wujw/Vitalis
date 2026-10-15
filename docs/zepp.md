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

力量 `lap_62` 的普通解析和流式解析共用同一行解析器及已核验动作表（`adapters/zepp/data/strength_exercises.json`）。APK、目录响应和观测证据核验过的代码可显示动作名称，并保留 `exercise_name_reference_mapping`；该表只覆盖已核验条目，不代表厂商全部枚举。没有代码、未知代码或暂定条目仍保留未知；名称映射不会补出重量单位、负重口径或处方资格。回归测试同时覆盖流式落库和普通解析，避免新增传输路径绕过映射。

同步状态区分成功、部分覆盖、需要重新认证、失败和未知；不能把一个流失败或没有密集文件理解成全库成功。迟到数据进入后续分析修订，缺失继续保持缺失。

HRV 请求使用包含首尾两天的本地日期窗口，再转换成 UTC 毫秒范围；同步 attempt 保存的时区会在 worker 恢复时继续使用，不从当前进程环境重新推断。持久同步保留原始事件和逐样本观测时间，mock/demo 路径只生成按本地日聚合的合成样本；两者都按报告时区归属 `calendar_day`。未知设备范围保持未知，不能把 mock 或厂商融合值描述成设备级观测。

## 协议和安全边界

当前代码按 ZeppBridge 公开行为适配区域端点、认证头和部分 payload，但 Vitalis 的来源/设备隔离、同步租约、覆盖状态和确定性分析是本项目边界。未知字段不自动变成已知指标，协议事实需要测试和第三方来源记录；相关第三方原文见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

不要把 APK、Cookie、apptoken、密码、验证码、真实响应或个人记录加入仓库。凭据由服务端加密保存，密钥与数据库分开；日志只保留非秘密分类。真实 Zepp 接入仍需在目标环境单独验收，mock 通过不能替代线上授权验证。
