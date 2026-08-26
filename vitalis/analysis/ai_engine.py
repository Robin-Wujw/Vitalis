"""AI Engine —— 只负责解释/总结/建议，绝不计算。

Pipeline 先由 RuleEngine + StatisticalEngine 算出结构化的
Decision + stats，AIEngine 只把「事实」打包给 LLM，让它生成自然语言。

规则：
1. 输入只含确定性的计算结果（score、趋势、命中规则）。
2. System prompt 强制：LLM 不得自行推断数字结论，只转述我们给的事实。
3. 未配置 LLM 时，用模板生成基于事实的说明（仍不计算）。
"""

import httpx

from vitalis.config import settings
from vitalis.models import Decision

SYSTEM_PROMPT = """你是 Vitalis Health Agent 的健康解释助手。
你只被允许做三件事：解释、总结、给训练建议。
你的输入是已算好的结构化健康事实（恢复分、HRV 趋势、训练负荷、命中规则）。
严禁虚构、修改或重新计算任何数字；你只能转述输入中给出的数值。
不要做医疗诊断；如果超出解释和建议范围，请说明这需要专业医生。"""


class AIEngine:
    """LLM 解释层。默认 OpenAI 兼容接口；未配置 key 时回退到模板化解释。"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ):
        self.api_key = api_key if api_key is not None else settings.llm_api_key
        self.base_url = base_url if base_url is not None else settings.llm_base_url
        self.model = model if model is not None else settings.llm_model

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def explain(self, decision: Decision, stats: dict) -> str:
        """生成自然语言解释。

        args:
            decision: RuleEngine 的确定性结论（唯一数字来源）
            stats: StatisticalEngine 的统计特征（唯一趋势来源）
        """
        facts = {
            "overall_recovery_score": decision.overall_score,
            "recovery_level": decision.recovery_level,
            "stress_level": decision.stress_level,
            "training_readiness": decision.training_readiness,
            "hrv_trend_pct": stats.get("hrv_trend_pct"),
            "load_30d": stats.get("load_30d"),
            "sleep_avg_7d_min": stats.get("sleep_avg_7d_min"),
            "matched_rules": decision.items,
        }
        if self.enabled:
            return self._llm_explain(facts)
        return self._template_explain(facts)

    # ---- LLM 路径 ----
    def _llm_explain(self, facts: dict) -> str:
        user_prompt = (
            "请用简体中文解释以下用户健康状态，并给出今日训练建议（不超过 150 字）。"
            "只能使用给定数值，不要计算新数字：\n" + _render_facts(facts)
        )
        try:
            resp = httpx.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.4,
                    "max_tokens": 300,
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as exc:  # LLM 失败不影响核心功能
            return self._template_explain(facts) + f"\n（LLM 调用失败，已用模板：{exc}）"

    # ---- 模板路径（无 LLM 时的确定性回退） ----
    @staticmethod
    def _template_explain(facts: dict) -> str:
        score = facts["overall_recovery_score"]
        lines = [f"当前恢复分 {score}（{facts['recovery_level']}），压力等级 {facts['stress_level']}。"]
        for item in facts["matched_rules"]:
            lines.append(f"· {item}")
        hrv = facts.get("hrv_trend_pct")
        if hrv is not None:
            lines.append(f"· HRV 7 天趋势 {hrv:+.0f}%")
        load = facts.get("load_30d")
        if load:
            lines.append(f"· 近 30 天训练负荷累计 {load}")
        readiness = facts["training_readiness"]
        advice = {
            "full": "今天状态很好，适合正常强度训练。",
            "moderate": "适合中等强度训练。",
            "easy": "建议只做低强度活动或恢复性训练。",
            "not_ready": "建议休息，优先睡眠与减压。",
        }
        lines.append(advice.get(readiness, ""))
        return "\n".join(l for l in lines if l)


def _render_facts(facts: dict) -> str:
    lines = []
    for k, v in facts.items():
        lines.append(f"- {k}: {v}")
    return "\n".join(lines)