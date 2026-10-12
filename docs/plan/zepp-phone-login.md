# 不用插件，在手机上连接 Zepp

[改动计划](README.md) · P0 · 前置：用户确认方案

## 为什么

现在连接 Zepp 要运维签发 manage 令牌、用 curl 建配对码，还要把插件 ID 写进 `VITALIS_PAIRING_ALLOWED_ORIGINS` 并重启 API；插件没有固定 ID，每台电脑装出来都不一样。用户要开发者模式加载插件、粘贴地址和配对码，而且只能在电脑上完成。插件实际只用一次：生产凭据 9/28 配对后没再更新，同步到 10/12 一直成功。

Zepp 官方网页登录（`user.zepp.com/universalLogin`）对手机号账号使用“手机号 + 密码”：先 `POST api-user.huami.com/registrations/{账号}/tokens` 换 access code，再 `POST account.huami.com/v2/client/login` 换 `app_token`、`login_token` 和 `user_id`。验证码只用于注册和重置密码。

## 改什么（提案）

- 配对链接由 `vitalis connect zepp` 命令或 Hermes 生成，发到手机。
- 链接打开 Vitalis 的手机页：输入 Zepp 手机号（或邮箱）和密码，服务端按官方网页相同的两步换取令牌，验证后加密保存；密码只在内存中用一次，不落库、不进日志和响应。
- 没有密码的账号：页面链接到 Zepp 官方“忘记密码”，用手机验证码设置密码后回来登录。
- 令牌到期前用 `login_token` 续期（接口先在真实账号上验证）；续期失败时通过 PushPlus 发重新登录链接。
- 浏览器插件和书签入口：手机页在生产稳定后删除。
- 安全原则从“账号密码只在官方页面输入”改为“密码只经过自己的 Vitalis 服务器一次，不保存”，同步更新 `SECURITY.md` 和 [Zepp](../zepp.md)。

## 验收

- 合成测试覆盖登录成功、密码错误、频率限制（429）、需要图形验证码和续期；密码、令牌不出现在日志、数据库明文和响应里。
- 用真实账号在手机上走通一次，首次回填和首份报告正常。
