# Vitalis Balance 2 桥接应用

[English](README.en.md)

仅适用于 Balance 2 的 Zepp OS 备用方案。Helio Strap 无法运行 Zepp OS 应用，因此此通道不支持 Helio Strap。

后台 app service 是版本化 NDJSON 日志的唯一写入方。每个有效的 `HeartRate.onCurrentChange()` 回调都会获得一个稳定的本地 sample ID，并通过一次文件写入追加到日志中；回调路径不会解析、排序或替换整个队列。Zepp 未说明固定的回调频率，因此这些记录是回调级测量值，并非有保证的 1 Hz 数据流。Zepp OS 不提供 `fsync` 保证，因此桥接应用会报告追加和恢复状态，而不会声称存储能够抵御断电造成的数据丢失。

前台页面拥有独立且可恢复的确认 checkpoint，并且每次上传一个固定的 high-water 快照。v2 服务端响应会结算精确的 sample ID：已提交或重放的样本以及明确的永久拒绝项会离开待处理队列；超时、格式错误的响应、身份验证失败和服务器错误则会保留这些样本。延迟执行的 service 维护会修复日志末尾被截断的行、压缩已结算的记录，并保留最新 3,600 条待处理记录，同时报告容量损失、永久拒绝和损坏计数器。service 使用 Zepp OS 文档规定的 `file` 属性启动。有意不采集 Accelerometer，因为后台 app service 不支持高功耗传感器。

## 设置

1. 将 `app.json` 中的占位 `app.appId` 替换为开发者账户所拥有的 app ID。
2. 在 Vitalis 中，使用 `X-User-Id` 调用 `POST /api/v1/connect/zepp/device-link` 并保留返回的 device token；服务器仅存储其 SHA-256 摘要。
3. 在 Zepp 应用设置中，输入 Vitalis 的公网 HTTPS base URL 和 device token。
4. 使用 Node 24 和 npm 11，安装锁定版本的依赖项，在 Balance 2 上预览，授予心率/后台权限，然后启动后台采集。应用以 Zepp OS API 4.2 为目标；最新发布的 `@zeppos/device-types` 仍为 4.0.0。

```bash
npm ci
npm test
npm run preview
npm run build
```

定期打开设备端应用并选择 `同步到 Vitalis`，以清空有界队列。云同步仍是历史数据的事实来源。
