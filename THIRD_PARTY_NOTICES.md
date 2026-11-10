# 第三方声明

[English](THIRD_PARTY_NOTICES.en.md)

> 本英文声明为权威版本，中文译文仅为便于阅读。下方 MIT 许可证正文具有权威性，并在两种语言文件中逐字转载。

## ZeppBridge 集成来源

`src/vitalis/adapters/zepp/` 中的部分 Zepp 云端端点、payload 规范化和区域探测约定是在参考 ZeppBridge 的基础上开发的。

- 上游仓库：<https://github.com/lingcang728/ZeppBridge>
- 已审查版本：`v2.1.0`（2026-09-02）；现有已转化约定所对应的精确历史源码 revision 尚未还原。
- 多设备对照基线：ZeppBridge `main` 的 `1012a00abbb41865e8abcfb8f59b3d264fbafe1b`（2026-11-02）。独立实现来源范围、同条目录别名与记账占位处理；参考 `normalizer/device.rs`、`normalizer/band.rs`、`storage/devices.rs` 及测试，不复制其按最长片段猜设备的策略，不把上游 `UserFused` 枚举当作厂商融合算法证明。
- 先前比对基线：ZeppBridge `main` 的 `fd53afd9fd95389bb1a5192af561a6514e9855c5`
  （2026-09-23）。本次按行为和回归测试借鉴、未整段复制以下上游修复：心率游标单位归一化
  （`c491d2b8b455`）、ODI/OSA 本地日历日期请求（`afb130a09cad`）、SpO2 有界分窗与响应上限处理
  （`592b685b652e`），以及 readiness 缺失值 sentinel 处理（`37e0b52b1425`）。
- 明确引用 ZeppBridge 的 Vitalis 模块：`auth_parser.py`、`client.py`、`fetcher.py`、`parser.py`、`sync_manager.py` 和 `entrypoints/api/routes/connect.py`。
- Vitalis 特有变更：Python/FastAPI/SQLAlchemy 架构、类型化规范合同、source/scope/device 来源信息、有界 chunk 编排、lease、retry、诊断和确定性分析边界。
- 上游许可证：MIT。ZeppBridge 还在其上游声明文件中标明了衍生自 Apache-2.0 的锻炼解码。复制或改编更多上游代码之前，必须记录精确 revision，并保留所有适用的 MIT 和 Apache 声明。

本条目记录来源，并不声称 Vitalis 与 ZeppBridge 或 Zepp Health 存在从属、获得其背书或在功能上等同。

## zepp-mcp 协议参考

2026-11-10 审查 [DhavalBhimani44/zepp-mcp](https://github.com/DhavalBhimani44/zepp-mcp) 的 `904cc301d63a64da6f633dd8a75b2a612e67240a`，并复核 ZeppBridge `a6a805dce7fba941af544e36d251c6d6026a1a21`。参考本人历史体重端点、exertion 与 Charge/readiness 原始流以及睡眠/训练补充字段，再经独立实现和已授权只读接口核查建立有界合同；没有复制上游代码或个人数据。zepp-mcp 上游许可证为 MIT。上游列出某工具不证明账号有相应数据，也不证明未解码内容可作为健康指标。

## zepp-dashboard 行为参考

2026-11-02 对照 [J-solan/zepp-dashboard](https://github.com/J-solan/zepp-dashboard) 的公开 `main` 文档与源码，独立实现午睡连续分段、readiness 修订时间、日负荷与七日累计分离、自动活动标记与厂商训练效果/心率区间字段，并参考其真实时间轴、缺测留白、日级趋势和分项 Charge 展示。没有复制上游代码、人体图片、前端资源或个人数据；人体图为本项目自行编写的简化 SVG。上游为 MIT；其单设备/欧区验证不构成 Vitalis 实测证据。Hevy CSV 仅由用户主动上传，不接入上游账户或服务。

## Zepp 厂商目录

`src/vitalis/adapters/zepp/data/` 的规范化映射来自用户提供的 Zepp 10.8.7-play APK 三份运动目录及官方 `/sport/config/muscle` 中文动作目录。只保留互操作所需的编号、名称、交叉关系和来源摘要；不分发 APK、图片、反编译实现或个人响应。208 个运动模式与 1,133 个动作名称的来源、占位排除及更新方法见 [Zepp](docs/zepp.md)。这些厂商标签不表示用户确认或 Vitalis 对训练动作的处方资格。

## Healthy Fitness Coach 知识适配

- 上游仓库：<https://github.com/Surge-Dan/healthy-fitness-coach-skill>
- 已审查提交：`d4471a86b2edc8dedd2d86ad115476f9edd43a5a`。
- 衍生范围：`skills/healthy-fitness-coach/` 中的知识型入口与六份中文教学参考；逐文件来源和改写范围见该目录的 `provenance.json`。
- 上游许可：MIT，Copyright (c) 2026 梅乃丹；完整原文随包保存在 `skills/healthy-fitness-coach/LICENSE`。
- Vitalis 适配移除了执行脚本、训记连接器、自动个人档案、训练 DNA、独立评分及图片功能。上游的第三方图片、依赖和数据不在本次分发范围内。
- 本适配不代表上游完整功能、医疗验证、来源作者背书或个人健康算法资格；上游教学资料不能覆盖 Vitalis 已保存事实及授权边界。

## OpenStrap analytics

Open Health Insights shadow 算法的部分内容移植或改编自 OpenStrap analytics。

- 上游仓库：<https://github.com/OpenStrap/analytics>
- 上游 revision：`45d72ed989c004008b919b366cd5ceda7061b7df`
- Vitalis 模块：`src/vitalis/intelligence/open_health/ewma.py`、`readiness.py`、`anomaly.py`、`sleep.py` 和 `load.py`。
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
