# 安全与隐私

[文档导航](docs/README.md) | [部署与恢复](docs/operations.md)

Vitalis 处理可识别的健康数据和第三方登录凭据，仍处于预发布阶段，未宣称合规认证或外部安全审计。不要在 issue、讨论、PR、日志或测试固定样例中提供真实账户、报告、访问令牌、Cookie、备份、设备标识或 APK。发现漏洞时，优先使用仓库托管平台的私密漏洞报告渠道；未启用时私下联系维护者。在修复并同意披露范围前，不公开可利用细节。

HTTP 请求必须带与本地用户绑定的 Bearer 令牌和所需 scope；`X-User-Id` 只做一致性检查，不能单独认证。浏览器配对码是一次性流程凭据，不是报告读取令牌。插件配对时 Zepp 密码和验证码只输入官方页面。手机登录页让密码经过用户自己的 Vitalis 服务端一次（只在该次请求的内存里，不写入数据库、日志、响应、URL 或模型上下文），这一原则变更已于 2026-10-13 经用户确认；配好公网 HTTPS 入口前不启用。真实部署应验证 TLS、用户绑定、配对回调、来源白名单和客户端令牌隔离；本机回环监听、CORS 和扩展 host 权限都不等于公网防护。

保存非空厂商凭据必须配置 `VITALIS_TOKEN_ENCRYPTION_KEY`，并将密钥与数据库分开保存；无效或缺失密钥时真实连接应被拒绝。数据库、备份、SQLite sidecar、`.env` 和投递状态限制文件权限。启用手机登录后，它产生的 `app_token` 与 legacy `login_token` 只能以加密 `AuthToken` 字段保存，后者在官方续期语义验证前不执行续期；到期或续期语义未知时标成 `needs_reauth`，不能伪造 refresh 成功。访问令牌只在库中保存摘要，不放进 shell 历史、URL、模型上下文或健康通知日志；签发时用 `--output` 写入仓库外的新私密文件。

同步撤销、旧 worker fencing、OAuth state 消费和配对尝试窗口属于安全边界。回调或 PushPlus 受理不能改变目标完成、健康反馈或用户计划状态。网络超时等不确定投递保留 `uncertain`，不能盲目重试。请参阅[运维](docs/operations.md)、[数据合同](docs/data-contracts.md)和[智能体集成](docs/agents.md)。

Vitalis 本身当前没有声明许可证；第三方来源和原始许可文本见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
