# Zepp 连接与同步

[文档中心](README.md) | [快速开始](quickstart.md) | [数据合同](data-contracts.md) | [扩展指南](../clients/browser_extension/README.md)

Vitalis 当前支持 Zepp 作为外部数据源。`ZEPP_MOCK=true` 是默认离线 mock；真实连接使用官方网页登录会话和区域云端接口。mock 验收不等于真实账户或真实 Hermes/PushPlus 联调。

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

## 同步与任务

API 只创建同步任务；独立 `vitalis worker` 执行厂商请求、分块、租约、重试和覆盖账本。创建任务时带 `Idempotency-Key`，可用明确本地日期 `from`/`to` 或 1–730 天窗口：

```text
POST /api/sync-jobs
Authorization: Bearer <sync-token>
Idempotency-Key: <new-or-reused-key>
{"from":"2026-10-01","to":"2026-10-07","source":"zepp"}
```

响应中的 `job_id` 只表示已入队。用 `GET /api/jobs/{job_id}` 查看 attempt/chunk 状态，用 `GET /api/data-status` 查看覆盖和最近结果。取消需要 `sync` scope 的 `POST /api/jobs/{job_id}/cancel`。来源撤销使用 `manage` scope 的 `POST /api/sources/zepp/revoke`；撤销保留本地事实，但 fencing 旧 worker，不能把旧凭据状态写回。

同步状态区分成功、部分覆盖、需要重新认证、失败和未知；不能把一个流失败或没有密集文件理解成全库成功。迟到数据进入后续分析修订，缺失继续保持缺失。

## 协议和安全边界

当前代码按 ZeppBridge 公开行为适配区域端点、认证头和部分 payload，但 Vitalis 的来源/设备隔离、同步租约、覆盖状态和确定性分析是本项目边界。未知字段不自动变成已知指标，协议事实需要测试和第三方来源记录；相关第三方原文见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

不要把 APK、Cookie、apptoken、密码、验证码、真实响应或个人记录加入仓库。凭据由服务端加密保存，密钥与数据库分开；日志只保留非秘密分类。真实 Zepp 接入仍需在目标环境单独验收，mock 通过不能替代线上授权验证。
