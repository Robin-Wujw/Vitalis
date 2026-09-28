# Agent 与客户端接入

[文档导航](README.md) | [数据合同](data-contracts.md) | [产品 Skill](../skills/vitalis/SKILL.md)

## 框架中立的合同

Vitalis 通过当前 `/api` 和 `/openapi.json` 提供用户范围的健康数据产品。Agent 只负责选择操作、解释已保存的结构化结果并提交用户明确给出的反馈；不得重新计算数据质量、趋势或训练处方。先用 `GET /api/data-status` 判断来源与覆盖；`GET /api/reports/{kind}` 只读已有结果，缺少指定日快照时返回 `404`，不会暗中同步。分析用 `POST /api/analysis-runs`、同步用 `POST /api/sync-jobs` 明确提交持久任务，拿到 ID 后查 `GET /api/jobs/{job_id}`。训练、反馈与 Bridge 上传的当前路径和参数以[自动生成的 Skill 子集](../skills/vitalis/references/api.md)及服务 OpenAPI 为准。

服务端 Bearer 令牌绑定一个本地用户及期限，按操作分配 `read`、`analyze`、`sync`、`feedback` 或 `manage` 范围。`X-User-Id` 只能与令牌所属用户核对，不能提升权限；浏览器配对码和设备上传令牌具有不同的用途。令牌由本地 CLI 创建并安全保存，见[运维](operations.md)。不要把令牌、个人健康记录、Zepp Cookie 或错误正文加入模型提示、URL、日志及仓库。

同步请求必须使用 `POST /api/sync-jobs`、`Idempotency-Key` 和 `sync` 范围；请求可用 `days`（1..730）或明确的 `from`/`to` 本地日期窗口，返回 `202` 后由 worker 执行。状态和取消分别使用用户范围的 `GET /api/jobs/{job_id}` 与 `POST /api/jobs/{job_id}/cancel`；跨用户任务不会泄露存在性，也不能取消。旧的 `POST /api/connect/zepp` 和隐藏 GET 别名已移除；连接、配对和凭据导入仍使用各自的 `/api/connect/zepp/*` 当前路径。

## 独立安装产品 Skill

将整个 `skills/vitalis/` 文件夹安装到框架的 Skill 目录即可；其 [SKILL.md](../skills/vitalis/SKILL.md)、`scripts/vitalis_api.py` 和 `references/api.md` 仅依赖同目录文件与 Vitalis HTTP API，不导入后端包、数据库或开发文档。私有环境变量是 `VITALIS_API_BASE_URL`（无路径的服务源地址）和 `VITALIS_ACCESS_TOKEN`（最小权限 Bearer 值）。脚本用 Python 标准库；读取可有限重试，写入不自动重试；拒绝带认证的重定向、无效源地址和跨来源目标。Analyze/Sync 要求持久幂等键文件；反馈通过 stdin 传 JSON，并建议带 `--key-file` 以便不确定结果可用相同内容安全重试；命令行不出现令牌或健康备注。

离仓复制文件夹并连接本地 mock HTTP 服务的 Read / Analyze / Act 与失败分支属于**自动化合同测试**。Hermes 实际运行环境的安装、发现、权限注入及一次真实调用属于单独的手动 smoke test；未提供已授权运行环境时标记 BLOCKED，不能用 mock 通过代替。其他 Agent 框架复用相同 HTTP 合同，而不是复制 Python 健康算法或要求 Hermes 内部对象。

根 [AGENTS.md](../AGENTS.md) 只指导仓库开发，不是产品 Skill；本页只记录集成边界，不担任 coding-agent 重构任务清单。
