# Claude 工作入口

Vitalis 的仓库级规范唯一维护在 [AGENTS.md](AGENTS.md)。开始任何修改前先读取它，再读取 [docs/plan/README.md](docs/plan/README.md) 和受影响领域的主责文档。

项目处于预发布阶段，允许破坏性更新；不要添加旧版 API、数据库或报告格式兼容层。按 [docs/deployment.md](docs/deployment.md) 的顺序发布：本地验证 → 服务器候选验收并切换全部服务 → 合并推送 `main` → 清理已合并分支。

不要把本文件扩展成第二份规范。规则变化只修改 `AGENTS.md`。
