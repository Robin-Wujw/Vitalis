# 后端测试超出 600 秒上限

[改动计划](README.md) · P0 · 相关：[发布与部署 SOP](../deployment.md)

## 为什么

2026-10-12 本地 `tools/check.py all --ci` 的 backend 段在 600 秒处超时，main 和候选都一样。原因是 `tests/test_api.py::test_health_range` 单独跑 4.5 秒，跟在前面的测试后面跑要约 460 秒：测试共用一份内存库，前面几个测试排队的同步没人消化，这个测试的 `_drain_sync_worker()` 一次替它们跑了 1,062 个同步块和 236 个分析任务。这一项让每次发布的本地检查都过不了，只能拆组跑。

## 改什么

- 每个测试只消化自己排的同步和分析任务，不替前面的测试买单；`test_health_range` 不再需要触发同步。
- 不靠调大超时解决。

## 验收

- `test_api.py` 整个文件回到 1 分钟以内，`tools/check.py all --ci` 在 600 秒上限内完整通过。
