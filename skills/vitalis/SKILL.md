---
name: vitalis
description: Use Vitalis Health Intelligence APIs for deterministic Chinese health analysis, training response, personal patterns, timelines, and explicit feedback actions.
---

# Vitalis 健康助手

通过本 Skill 目录中的 [独立 HTTP 客户端](scripts/vitalis_api.py) 读取 Vitalis 已有结果。只用 Python 3.11+ 标准库；把整个 Skill 目录复制到任意位置即可运行，命令中的脚本路径相对本文件所在目录，不依赖仓库、工作目录或旧版 `tools/`。接口范围见本目录的 [API 操作清单](references/api.md)。

## 私密配置与调用

- 在私有运行环境设置 `VITALIS_API_BASE_URL`（服务源地址，无路径、用户名、查询或片段；非本机 HTTP 必须改用 HTTPS）和 `VITALIS_ACCESS_TOKEN`（具备相应 `read`、`analyze`、`sync` 或 `feedback` 权限）。令牌只从环境读取，绝不放到命令行、日志、对话或版本库；不通过 `X-User-Id` 选择身份。
- 使用 `python <Skill目录>/scripts/vitalis_api.py <命令> ...`；工具输出 JSON。错误 JSON 中的 `status=error` 是失败，HTTP 401/403 是鉴权/权限问题，连接或协议错误不是无数据。不要把工具输出中的私人记录转发到不可信场所。
- 读取：`status` 查覆盖；`report daily|morning|evening|weekly|monthly|weekly-briefing|monthly-briefing [--day YYYY-MM-DD]` 查已生成报告；`workouts [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--limit N]` 查训练，详情附 `--id ID --source SOURCE`；`job ID` 查任务。更窄的已有事实与解释可用固定白名单 `query profile|trends|events|explain|context|training-responses|personal-model|personal-associations|timeline|training-preferences|feedback`（按需指定 `--day` 或 `--start` / `--end`）；不能传入任意 URL。读取不会启动分析或同步。
- 仅在用户明确要求重新分析时执行 `analyze --day YYYY-MM-DD --key-file <私密持久路径>`；仅在用户明确要求同步时执行 `sync --days N --key-file <私密持久路径>`。首次调用前客户端在该路径持久化 Idempotency-Key；同一任务、同一参数重试时复用**同一文件**，新任务使用新文件，不能把文件放在 Skill 或仓库中。请求超时或结果不明确时先查已知任务 ID；无 ID 时可用同一文件人工重试。客户端不会自动重试任何写请求。
- 仅在用户明确要求记录其本人提供的主观信息时，将一份 JSON 对象通过**标准输入**传给 `feedback --key-file <私密持久路径>`（令牌和备注均不经命令行参数）；例如 `{"notes":"今天较疲劳"}`。同一反馈的不确定重试复用同一键文件；省略键文件时不得自动重发。关联训练必须同时提供 `workout_id` 与 `workout_source`；RPE 还须关联已完成训练。其他明确写入使用固定白名单 `action profile-patch|preferences-put|preferences-patch|strength-confirm|recommendation-complete|event-acknowledge`；需要目标时指定 `--id`，力量确认还要 `--source`，其余输入经 stdin 传 JSON。用户资料修正携带当前 revision；遇到冲突重新读取，不覆盖其他更新。无幂等保证的写入在响应不明时不能盲目重发。
- 分析与同步只返回任务受理信息；用 `job ID` 查看状态，只有成功并已生成结果时再读报告。报告 `status=snapshot_missing` 只表示指定日期的快照不存在；不要改查别的日期、自动启动分析/同步或编造结果。其他 HTTP 404（如任务不存在）与 401/403、网络故障必须分别说明。

## 回答边界

- 所有面向用户的回答使用中文。只复述 API 返回的事实、来源、单位、观测时间、限制与原有建议；缺失不是零，陈旧或未完成不是当前结论。不要自行计算趋势、恢复、相关、训练处方或由较短周期拼接周/月结果。
- 优先使用 `*_label`、`*_labels` 和报告的 `sections`，不要直接呈现内部枚举或规则 ID。`INSUFFICIENT_DATA` 或仅事实版只说明已有事实与缺口，不提供推断训练决策。保持 `decision.action_plan` 中的主训练、可选训练、二者关系和停止条件，不能虚构动作、组数、心率或负重。被问及“为什么”时用只读 `query explain` 的已保存触发事实与门控，只引用结果实际返回的 `evidence_refs`，不把支持信号说成触发原因。
- 厂商睡眠评分、readiness、Charge 等只能按 API 明示的参考限制描述，不当作 Vitalis 判定；不同用户、设备、来源或单位的记录不得合并。未观测时段不是零，未知训练日不是休息日；未指明范围的热量不能当作总能耗或相加，没有摄入记录就不推断热量赤字。周报是滚动 7 日、月报是滚动 28 日而非自然月；部分合计不能写成完整总量，关联不代表因果。不得诊断疾病；紧急症状建议寻求专业医疗协助。
