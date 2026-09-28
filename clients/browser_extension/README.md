# Vitalis Zepp 登录扩展

[English](README.en.md)

此 Manifest V3 扩展是 Vitalis 云端配对流程中由用户操作的一端。

1. 打开 `chrome://extensions` 或 `edge://extensions`。
2. 启用开发者模式，选择 **加载已解压的扩展程序**，并选择此目录。
3. 按[当前快速开始](../../docs/quickstart.md)用 `manage` 令牌调用 `POST /api/connect/zepp/pair`，打开响应中的 `scan_url`，读取一次性配对码。
4. 在扩展中输入 Vitalis 地址和配对码，然后选择 **登录并连接**。
5. 在官方页面完成登录。扩展会自动继续配对。

当 Vitalis 地址不是 localhost 时，必须使用浏览器信任的 HTTPS。扩展对 `http://localhost/*` 和 `http://127.0.0.1/*` 使用固定本机 host 权限，不再请求动态本机权限；公网地址仍需 HTTPS 和动态站点授权。服务端必须在 `VITALIS_PAIRING_ALLOWED_ORIGINS` 中配置扩展详情页显示的 `chrome-extension://<扩展 ID>` 来源，并重启 API；Chrome 访问本机 API 时的 Private Network Access 预检也只对该白名单来源放行。只授权扩展访问 Vitalis 主机不等于服务端信任该扩展来源。

弹出窗口会按粘贴时的原样保存两个配对字段，因此在复制第二个值期间关闭并重新打开窗口不会丢失第一个值。开始新的配对码流程时会先清除扩展本地保存的旧浏览器链接令牌，避免把新库的配对请求误发往旧库的链接更新入口。

Cookie 发现仅使用获准 Zepp/Huami 域名上的已知 Zepp 登录 Cookie 名称，与 Cookie 的 URL 路径无关。

无法检测到现有登录时，弹出窗口会显示仅保留在本地的诊断信息，其中包含可见 Cookie 数量以及 Cookie 名称/域名对。该诊断信息绝不包含 Cookie 值，也不会上传至 Vitalis，并会在配对后清除。

当前 Zepp 页面可能会将有效的 `apptoken` 和用户 ID 保存在页面存储中，而非先前使用的登录 Cookie。列入允许列表的内容脚本仅从页面存储或 Zepp/Huami HTTPS 来源上页面自身的第一方 Cookie 视图中读取固定的凭据键，并将它们直接传递给后台 worker。这样可以覆盖扩展 Cookie API 不会返回的分区 Cookie 存储或特定于浏览器配置文件的 Cookie 存储。凭据绝不会写入扩展存储或日志。

配对会打开 Zepp 自有的 `universalLogin` 路由，并将 Watchface 应用作为其官方回调。因此，在页面存储桥接脚本运行前，已有的账户中心会话可以完成应用交接，而无需再次提示输入密码。

扩展仅会读取 Zepp/Huami Cookie。对非本机 Vitalis 来源的访问权限会在运行时请求，并且仅授予用户输入的确切来源；本机回环地址的固定 host 权限仍需服务端 Origin 白名单配合。扩展会在 Cookie 发生变化时以及每 30 分钟检查一次会话，通过可撤销的浏览器链接更新 Vitalis，并在需要重新登录时进行报告。
