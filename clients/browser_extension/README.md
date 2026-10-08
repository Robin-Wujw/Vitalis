# Vitalis Zepp 登录扩展

[English](README.en.md)

这是 Manifest V3 扩展，是 Vitalis Zepp 配对流程的浏览器端。它可独立随扩展目录打包，不依赖仓库文档路径。

## 配对

1. 在 Chrome 或 Edge 打开 `chrome://extensions` 或 `edge://extensions`。
2. 开启开发者模式，选择 **加载已解压的扩展程序**，选中本目录。
3. 在已经初始化的 Vitalis 服务上，用具备 `manage` scope 的 Bearer 令牌调用 `POST /api/connect/zepp/pair`。服务返回一次性配对码和 `scan_url`；只在官方 Zepp 页面打开该 URL。
4. 在扩展弹窗输入 Vitalis 服务源地址和一次性配对码，选择 **登录并连接**。
5. 在官方页面完成登录；扩展会继续配对并在需要重新登录时提示。

请求示例（令牌只从私密环境读取，不要粘贴到文档或日志）：

```bash
curl -X POST "$VITALIS_API_BASE_URL/api/connect/zepp/pair" \
  -H "Authorization: Bearer $VITALIS_ACCESS_TOKEN"
```

## 来源和网络要求

非 localhost 地址必须使用浏览器信任的 HTTPS。扩展对 `http://localhost/*` 和 `http://127.0.0.1/*` 有固定 host 权限；公网源站仍需运行时授权。服务端的 `VITALIS_PAIRING_ALLOWED_ORIGINS` 必须包含浏览器显示的精确 `chrome-extension://<扩展 ID>` 来源，并在修改后重启 API。扩展 host 权限不等于服务端信任或绕过 API 鉴权。

开始新的配对码流程会清除扩展本地保存的旧浏览器链接令牌，避免把新数据库请求发到旧数据库。弹窗按粘贴原样保存两个配对字段，关闭后不会丢失已粘贴的第一个字段。

扩展只读取允许的 Zepp/Huami 域名和已知登录 Cookie 名称，也支持官方页面存储中的固定凭据键。凭据不会写入扩展存储或日志。无法发现登录时，弹窗只显示本地的 Cookie 数量及名称/域名对，不显示 Cookie 值，也不上报该诊断；配对后会清除诊断。

登录凭据和健康数据属于敏感信息。只在官方页面输入密码和验证码；服务端、数据库和真实环境边界见 Vitalis 的安全与运维文档。
