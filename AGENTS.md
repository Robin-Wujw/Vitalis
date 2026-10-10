# Vitalis Codex / Claude 仓库执行规范

本文件是 Codex、Claude 和其他 coding agent 的唯一仓库开发与发布规范。`CLAUDE.md` 只负责入口跳转，不复制本文件；运行时产品 Skill 见 [skills/vitalis/SKILL.md](skills/vitalis/SKILL.md)。完整整改需求和阶段任务见 [docs/plan.md](docs/plan.md)。 本地依赖和检查见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 项目状态与破坏性变更

Vitalis 处于预发布阶段。当前任务明确允许破坏性更新，不需要旧版本 API、数据库 schema、报告格式、CLI 别名、双写、双读或迁移兼容层。修改前先检查 `git status`，只保留当前目标需要的源码、测试和文档；旧兼容代码、旧迁移脚本、重复 Markdown 和失效入口应删除，而不是继续包裹适配器。

删除真实数据库、健康记录、凭据、APK、图片或他人改动必须具备明确的针对性授权；已有会话授权不重复询问。数据库无变化时保留现库；可加字段解决时只加字段；确实需要破坏性迁移时按 [docs/deployment.md](docs/deployment.md) 备份、候选库、验收、切换，并在授权范围内删除被替代的旧工作数据库，保留校验过的回滚备份。不能把 `rm` 当作备份。

## 编码规范

- Python 3.11–3.13，依赖和命令使用锁定的 `uv` 环境。
- 模块只依赖职责下游；API、Skill、PushPlus 不重新计算健康事实。
- 新事实必须保留来源、单位、观测时间、获取时间和数据资格；缺失不补零，未知不推断。
- 日期必须明确 `calendar_day`、`sleep_day`、`activity_day`；所有比较记录窗口、样本数和 coverage。
- 原始数据、标准化事实、分析快照、公开报告投影和渠道排版分层保存，不让 Markdown renderer 访问数据库或重算。
- 输入写入、用户反馈、手动同步和晚到数据必须创建有影响范围的分析任务；历史 last-good snapshot 不得因全局 revision 消失。
- PushPlus 是单向输出，不提问、不把已读/沉默当反馈；Hermes 只有用户明确授权时才写入反馈、纠错或推荐完成。
- 厂商 readiness/Charge 和 shadow-only 分析必须保留来源标签，不能替代 Vitalis 的可解释决策。
- 接口错误要给出稳定的 `state`、`failure_code`、`retryable` 和 `next_action`，禁止用 200 加错误 body 冒充成功。

## 工作流程

1. 读 [docs/README.md](docs/README.md)、[docs/plan.md](docs/plan.md) 和受影响模块；先写或更新失败测试，再实现。
2. 本地修改只在 feature branch 完成；不直接在 `main` 上堆未验证改动。
3. 运行目标测试、文档检查、`git diff --check` 和按范围的 `tools/check.py`；如缺依赖或无法运行，原样记录退出码和阻塞项。
4. 检查 `git diff`、删除冗余文档/兼容代码、更新唯一主责文档和生成产物，再提交。
5. 固定 feature branch 的候选 commit SHA，推送候选分支或传输可校验的提交到服务器；先在隔离环境运行锁定依赖、目标测试、schema 和合成报告验收，不提前合并 `main`。
6. 按 [docs/deployment.md](docs/deployment.md) 备份数据库，按实际 schema 差异决定保留、加字段或破坏性迁移；把所有 Vitalis 服务切到同一个候选 SHA 和虚拟环境，验证 `/live`、`/ready`、`doctor`、worker 心跳、合成报告和实际加载路径。
7. 服务器验收通过后才合并并推送 `main`；核对最终 `main` 与已验收候选的源码树一致，服务器仓库同步到最终 SHA，所有服务和发布记录对齐，删除已合并的临时本地/远端分支后结束。任何本地或服务器校验失败都先修复并重验，部署失败回滚代码/环境并保留备份，不恢复旧兼容迁移链。

## 验证基线

```bash
uv run --locked --extra dev python -m pytest tests/architecture/test_documentation_layout.py tests/test_bilingual_markdown.py -q
uv run --locked --extra dev python tools/check.py docs
git diff --check
```

源码或行为改动继续运行对应的 `quick`、`backend`、`clients`、`package` 或 `all --ci`。只有看到本次命令的退出码和完整结果后，才能声称通过。测试只使用合成或脱敏数据，绝不把个人记录、令牌、Cookie、APK 或真实 PushPlus 内容写入仓库、日志和样例。
