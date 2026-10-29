"""Static coaching instruction contracts, not a claim of live model compliance."""

from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS = ("skills/vitalis/SKILL.md", "deploy/hermes/SOUL.md")


@pytest.fixture(params=DOCUMENTS)
def document(request):
    return (ROOT / request.param).read_text(encoding="utf-8")


def test_conversational_authority_and_personal_prescription(document):
    for rule in (
        "唯一的个人健康事实、指标和趋势引擎", "一般健康教育", "假设示例",
        "不得自行计算", "恢复分数", "不得覆盖", "已保存建议",
        "实际个人训练处方", "不能新编个体化方案",
    ):
        assert rule in document
    assert "只复述 API 返回" not in document
    assert "不在客户端重算或拼接" not in document


def test_general_personal_and_explanation_read_routes(document):
    for rule in (
        "一般知识不调用 API", "最少的相关只读命令", "组合",
        "report daily --view", "query explain", "query trends",
        "query training-responses", "workouts", "query profile",
        "query training-preferences", "query context", "status",
    ):
        assert rule in document


def test_natural_answers_and_evidence_labels(document):
    for rule in (
        "先回答当前问题", "不要求立即给出短答案", "1–2 个",
        "不重复询问", "API 事实", "用户陈述", "一般建议", "不确定性",
        "症状先分诊", "胸痛", "呼吸困难", "不等待工具", "不得诊断",
    ):
        assert rule in document


def test_no_unrequested_persistence_or_extra_data_access(document):
    for rule in (
        "明确", "feedback", "sync", "analyze", "资料", "不自动写入",
        "训练 DNA", "影子档案", "凭据", "数据库", "源码",
        "现有模型 API", "不向其他外部服务传输",
    ):
        assert rule in document


def test_coach_skill_is_knowledge_only(document):
    for rule in (
        "skills", "healthy-fitness-coach", "references", "外部脚本", "MCP", "xunji",
    ):
        assert rule in document


def test_useful_examples_cover_safe_routes(document):
    for question in (
        "什么是 RPE", "我今天适合练吗", "为什么建议减量", "最近有没有进步",
        "帮我排明天的训练", "我今天有点累", "请记录今天的疲劳",
    ):
        assert question in document
    assert "不是你的处方" in document
    assert "不会自动记录" in document


@pytest.mark.parametrize("question, required", (
    ("什么是 RPE", ("无需", "假设", "不是你的处方")),
    ("我今天适合练吗", ("report daily --view", "已保存建议", "缺")),
    ("为什么建议减量", ("query explain", "evidence_refs", "一般训练原理")),
    ("最近有没有进步", ("query trends", "query training-responses", "workouts", "coverage", "不")),
    ("帮我排明天的训练", ("已保存建议", "query profile", "query training-preferences", "一般", "不")),
    ("我今天有点累", ("用户陈述", "不会自动记录")),
    ("请记录今天的疲劳", ("明确", "feedback", "成功", "不", "评分")),
))
def test_examples_bind_questions_to_routes_and_limits(document, question, required):
    example = next(line for line in document.splitlines() if question in line)
    for phrase in required:
        assert phrase in example


def test_persona_is_reusable_and_uses_hermes_python():
    soul = (ROOT / "deploy/hermes/SOUL.md").read_text(encoding="utf-8")
    assert 'python "$HERMES_HOME/skills/vitalis/scripts/vitalis_api.py"' in soul
    assert "Hermes 虚拟环境" in soul
    assert "先通过 skills 工具读取 `vitalis`" in soul
    assert not re.search(r"\b20\d{2}-\d{2}-\d{2}\b|[A-Za-z]:\\|/opt/|/var/lib/", soul)
    assert "TELEGRAM_ALLOWED_USERS=" not in soul
    assert "VITALIS_ACCESS_TOKEN=" not in soul
