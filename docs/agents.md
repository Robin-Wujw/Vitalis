# 智能体与客户端集成

[文档中心](README.md) | [快速开始](quickstart.md) | [报告与渠道](reports.md) | [产品 Skill](../skills/vitalis/SKILL.md)

本页区分仓库中的产品 Skill、HTTP API 和可选 Hermes 对话入口。Skill 是离仓可复制的薄客户端，不依赖仓库根目录；它不实现健康算法、不绕过鉴权，也不要求 PushPlus 报告回复。

## 鉴权和身份

用户范围请求必须带与本地用户绑定的 `Authorization: Bearer <token>`。令牌由 `vitalis token issue` 签发，scope 包括 `read`、`analyze`、`sync`、`feedback` 和 `manage`；只授予完成当前动作所需的最小 scope。`X-User-Id` 只能做一致性检查，不能代替 Bearer 认证。浏览器配对码和 browser-link token 不是 API 读取令牌。

非本机 Skill API 源必须使用 HTTPS；客户端拒绝含路径、凭据、查询或片段的 origin，并拒绝重定向。令牌从 `VITALIS_ACCESS_TOKEN` 私密环境读取，不放命令行、URL、日志或对话。服务端错误会清洗供应商响应和数据库内容。API 使用真实的 4xx/5xx 状态码，错误正文统一返回 `state=failed`、`failure_code`、`message`、`retryable`、`next_action` 和 `request_id`。`next_action` 区分重新认证、补齐权限、修正请求、刷新状态后重试、稍后重试和检查服务；不能把失败正文当作成功结果或健康观测。

## Skill 的读写合同

将 `skills/vitalis/` 目录复制到任意位置，在私有环境设置 `VITALIS_API_BASE_URL` 和 `VITALIS_ACCESS_TOKEN`，运行：

```bash
python /path/to/vitalis/scripts/vitalis_api.py report daily --day 2026-10-07
```

固定读取命令包括 `status`、`report`、`workouts`、`job` 和白名单 `query`。`report daily --day YYYY-MM-DD --state` 读取报告新鲜度、`last_good_snapshot`、任务及下一步；更新期间可以呈现同日期上一份成功事实，并明确它正在更新。读取不会启动同步或分析；`report` 的 404 会转换为 `status=snapshot_missing`，不能改查其它日期或编造结果。完整操作表由 `tools/generate_api_reference.py` 从当前 OpenAPI 生成，见 [references/api.md](../skills/vitalis/references/api.md)。

`analyze`、`sync` 和 `feedback` 只有用户明确要求才调用。分析/同步写请求必须用持久 `--key-file`；同一个任务、参数和不确定响应复用同一键，客户端不会自动重试写请求。反馈 JSON 从标准输入传入，不能把备注放命令行；关联训练需要 `workout_id` 与 `workout_source`，RPE 还需要完成训练。资料 patch 使用 revision，冲突先重新读取。写入只改变明确的用户输入或反馈，不由发送、打开或沉默推断完成。

## 回答规则

客户端回答使用中文，优先呈现 API 返回的 `*_label`、报告 `sections`、单位、来源、观测时间和局部缺口。缺失不是零，未知训练日不是休息日，厂商 readiness/Charge 不当作 Vitalis 判定。不得自行计算趋势、相关、恢复或训练处方；不得由短窗口拼成周/月结果；不得诊断疾病。`open_health_insights` 只能按 `shadow_only` 描述。

正常报告先回答当前问题，不泄漏 PushPlus 配置或令牌。Hermes 只有在用户明确授权时才能写反馈、资料、推荐完成或事件确认；没有对话要求时不主动追问。报告生成始终使用确定性引擎和已保存事实，不自由生成百分比、诊断、HTML、链接、按钮或 CSS。

## 验收边界

离仓 Skill 的 mock 和合同测试只证明客户端边界、Bearer 处理和 JSON 合同；不证明真实 Hermes 环境、真实 Zepp 账户、浏览器扩展或 PushPlus 已联调。实际接入要在新临时目录使用合成数据，保存任务状态但不发送真实通知。遇到 401/403、404、网络错误、未完成任务和 `uncertain` 投递分别说明，不把其中任一项写成无数据或成功。
