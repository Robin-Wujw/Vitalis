# 参与开发

[开发入口](AGENTS.md) | [文档职责](docs/README.md)

## 本地准备

需要 Python 3.11-3.13；从仓库根目录安装开发依赖，使用新建或可丢弃的 SQLite 库测试。生产用户数据、供应商令牌、日志和用户提供的 APK 不用于样本或 CI。

```bash
python -m pip install 'uv==0.12.9'
uv sync --locked --extra dev
uv run --locked --extra dev python -m vitalis --help
```

`uv sync --locked --extra dev` 创建 `.venv`；运行下面的裸 `python` 命令前，Linux 用 `source .venv/bin/activate`，Windows PowerShell 用 `.venv\Scripts\Activate.ps1` 激活。当前包目录为 `src/vitalis/`，命令行入口是 `vitalis` 或 `python -m vitalis`；运行真实服务和创建新库见[快速开始](docs/quickstart.md)。不要把临时开发库误当备份；`db reset` 需明确目标和确认且拒绝当前配置库。

## 验证与变更

```bash
python -m pytest tests/architecture/test_documentation_layout.py tests/test_bilingual_markdown.py -q
python tools/check.py docs
python tools/check.py quick
git diff --check
```

`python tools/check.py backend`、`clients`、`package` 和 `all --ci` 按改动范围运行；`package` 会构建并检查 wheel/sdist 内容、在离仓环境安装 wheel，验证第三方声明随包提供且本地 APK/图片不进入源分发包。`all --ci` 汇总上述目标及离线端到端验收；检查前需准备锁定依赖、`uv`、Node.js 和离线缓存。缺少工具或依赖时如实记录阻塞，不能把未运行的部分写成通过。提交说明应列行为变化、目标测试与文档更新；失败和跳过写明原因。示例输入用合成或脱敏记录，保留缺失字段、来源、单位和训练身份；协议修改同时更新对应测试及客户端。按[文档中心](docs/README.md)的主题归属更新唯一当前指南，不以已删除的旧双语契约或历史笔记代替当前源码；文档历史可在 Git 中追溯。许可证材料见[第三方声明](THIRD_PARTY_NOTICES.md)。
