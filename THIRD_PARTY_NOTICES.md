# 第三方声明

[English](THIRD_PARTY_NOTICES.en.md)

> 本英文声明为权威版本，中文译文仅为便于阅读。下方 MIT 许可证正文具有权威性，并在两种语言文件中逐字转载。

## ZeppBridge 集成来源

`vitalis/connectors/zepp/` 中的部分 Zepp 云端端点、payload 规范化和区域探测约定是在参考 ZeppBridge 的基础上开发的。

- 上游仓库：<https://github.com/lingcang728/ZeppBridge>
- 已审查版本：`v2.1.0`（2026-09-02）；现有已转化约定所对应的精确历史源码 revision 尚未还原。
- 明确引用 ZeppBridge 的 Vitalis 模块：`auth_parser.py`、`client.py`、`fetcher.py`、`parser.py`、`sync_manager.py` 和 `api/routes/connect.py`。
- Vitalis 特有变更：Python/FastAPI/SQLAlchemy 架构、类型化规范合同、source/scope/device 来源信息、有界 chunk 编排、lease、retry、诊断和确定性分析边界。
- 上游许可证：MIT。ZeppBridge 还在其上游声明文件中标明了衍生自 Apache-2.0 的锻炼解码。复制或改编更多上游代码之前，必须记录精确 revision，并保留所有适用的 MIT 和 Apache 声明。

本条目记录来源，并不声称 Vitalis 与 ZeppBridge 或 Zepp Health 存在从属、获得其背书或在功能上等同。

## OpenStrap analytics

Open Health Insights shadow 算法的部分内容移植或改编自 OpenStrap analytics。

- 上游仓库：<https://github.com/OpenStrap/analytics>
- 上游 revision：`45d72ed989c004008b919b366cd5ceda7061b7df`
- Vitalis 模块：`vitalis/intelligence/open_health/ewma.py`、`readiness.py`、`anomaly.py`、`sleep.py` 和 `load.py`。
- 上游衍生范围：winsorized EWMA 约定、夜间 lnRMSSD readiness 结构、稳健 median/MAD anomaly 约定、Banister TRIMP 和 ATL/CTL/TSB 指数负荷约定。
- Vitalis 特有变更：类型化 `OpenHealthInsights 1.0` envelope、source/device 隔离、用户确认的 profile gate、coverage/refusal policy、hard-reject 与 stale 处理、SWC readiness band、99.9% anomaly 阈值与 persistence policy、睡眠 timing/regularity policy、锻炼 pause/gap 处理、上游 coverage 下限、非诊断性措辞，以及与 Decision Policy 7.0 隔离的 `shadow_only`。

Vitalis 不声称等同于 WHOOP、OpenStrap 硬件或任何专有 recovery/readiness score。这些算法是描述性的个人统计辅助工具，不构成医疗诊断或治疗建议。

### MIT License

Copyright (c) 2026 OpenStrap

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
