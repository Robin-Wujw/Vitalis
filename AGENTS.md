# Working on Vitalis

这是仓库开发入口，不是运行时健康助手的产品 Skill。产品调用规则见 [skills/vitalis/SKILL.md](skills/vitalis/SKILL.md)；客户端鉴权边界见 [docs/agents.md](docs/agents.md)。

## 范围

项目处于预发布阶段。遵循当前源码和测试，不把设计目标当成已实现行为。共享工作区中的数据库、健康记录、凭据、APK、图片和他人改动必须保留，不自行删除、重置、发布、推送或发送真实消息。

源码在 `src/vitalis/`。接口层不重复实现健康算法；新事实要有来源、单位和时间资格，缺失项不补零，不推断未知动作或用户反馈。HTTP 身份由 Bearer 令牌决定，`X-User-Id` 不能代替鉴权；调度器仅由独立 worker 启动。

## 先读什么

- 环境、检查和改动范围：[CONTRIBUTING.md](CONTRIBUTING.md)
- 模块、数据流和信任边界：[docs/architecture.md](docs/architecture.md)
- 数据资格、单位和纠错：[docs/data-contracts.md](docs/data-contracts.md)
- Zepp 适配边界：[docs/zepp.md](docs/zepp.md)
- 运行、备份和诊断：[docs/operations.md](docs/operations.md)
- 文档职责：[docs/README.md](docs/README.md)

## 验证

先运行受影响的目标测试，再按范围运行：

```bash
uv run --locked --extra dev python -m pytest tests/architecture/test_documentation_layout.py tests/test_bilingual_markdown.py -q
uv run --locked --extra dev python tools/check.py docs
git diff --check
```

源码或行为变更还应运行对应的 `python tools/check.py quick`、`backend`、`clients`、`package` 或 `all --ci`。说明实际命令、通过/失败/跳过和阻塞；不能把未运行的检查写成通过。生成文档要修改生成源并重新生成，第三方 notices 原文保留。测试样例只能使用合成或脱敏数据，不能把个人记录、凭据或 APK 放入仓库。
