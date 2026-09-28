# Working on Vitalis

这是仓库开发与 coding agent 的入口，不是运行时健康助手的产品 Skill。产品使用流程由 [skills/vitalis/SKILL.md](skills/vitalis/SKILL.md) 定义；客户端当前鉴权差距见 [docs/agents.md](docs/agents.md)。

## 范围

项目处于预发布阶段；改动需遵循当前源码与测试，而不是历史文档。共享工作区中保留他人更改和用户资料，不自行删除、重置、发布、推送或发送真实消息。数据库、个人健康记录、凭据和 APK 均不得进入提交、测试固定样例或日志。

## 按需阅读

- 开发环境、检查与提交范围：[CONTRIBUTING.md](CONTRIBUTING.md)。
- 当前模块、数据流与信任边界：[docs/architecture.md](docs/architecture.md)；运行与诊断见 [docs/operations.md](docs/operations.md)。
- 数据资格与纠错：[docs/data-contracts.md](docs/data-contracts.md)。
- Zepp 协议证据：[docs/zepp.md](docs/zepp.md)。
- 客户端与产品 Skill 的界限：[docs/agents.md](docs/agents.md)。
- 部署、备份和同步排障：[docs/operations.md](docs/operations.md)。
- 未完成目标单独查阅 [docs/plans/rebuild.md](docs/plans/rebuild.md)，不得作为已实现行为。

## 边界与验证

- 源码在 `src/vitalis/`；先核对相关源码、测试及 `git status`。接口层不重复实现健康算法；新事实需要有来源、单位和时间资格，缺失项不补零，不推断未知动作或用户反馈。
- HTTP 用户身份由 Bearer 令牌决定，`X-User-Id` 不能代替鉴权；调度器仅由 worker 启动。只改受影响的模块及其当前消费者。
- 先运行目标测试，再运行相应的 `python tools/check.py quick`、`python tools/check.py docs` 或更宽目标。文档布局测试：`python -m pytest tests/architecture/test_documentation_layout.py -q`；说明命令、通过/失败/阻塞及未验证项。`git diff --check` 检查变更空白。
- 修改行为时更新 [docs/README.md](docs/README.md) 列出的主题主责文档；不复制新的系统总纲。安全报告方式见 [SECURITY.md](SECURITY.md)。
