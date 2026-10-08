# 参与开发

[开发入口](AGENTS.md) | [文档职责](docs/README.md)

## 本地准备

需要 Python 3.11–3.13。仓库根目录安装锁定依赖，并使用新建或可丢弃的 SQLite 库：

```bash
python -m pip install 'uv==0.12.9'
uv sync --locked --extra dev
uv run --locked --extra dev python -m vitalis --help
```

Windows PowerShell 可激活 `.venv\Scripts\Activate.ps1`；Linux 使用 `source .venv/bin/activate`。CLI 入口是 `vitalis` 或 `python -m vitalis`。不要把临时开发库当备份；`db reset` 只接受明确目标和确认，并拒绝当前配置库。

## 修改与检查

源码改动先确认 `git status`，只改受影响模块及当前消费者。数据、凭据、日志和用户提供的 APK 不用于样本或 CI。接口只读取已保存结果或创建持久任务；健康算法留在 intelligence/application 层。

```bash
python -m pytest tests/architecture/test_documentation_layout.py tests/test_bilingual_markdown.py -q
python tools/check.py docs
python tools/check.py quick
git diff --check
```

按改动范围运行 `backend`、`clients`、`package` 或 `all --ci`。`package` 会检查 wheel/sdist 内容和第三方声明；缺工具或依赖时如实记录阻塞。修改生成文档时改生成源后运行生成器，并检查生成文件无漂移。修改协议时同步更新对应测试和[唯一主责文档](docs/README.md)。提交说明列出行为变化、测试结果和文档更新；本仓库没有声明 Vitalis 自身许可证，第三方材料见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
