"""Static behavior-policy ownership checks, not live model compliance tests.

These regressions inspect the policy the agent receives. Deployment additionally
requires synthetic multi-turn evaluation of actual answers and tool traces.
"""

from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def test_persona_prioritizes_task_completion_and_concise_answers():
    soul = _read("deploy/hermes/SOUL.md")
    for rule in (
        "完成当前任务", "同一轮", "不能只说", "可以查询", "200～500 字",
        "完整计划", "表格", "不重复询问", "已知", "目标", "频率",
        "安全问题", "具体安排之前", "条件性", "适中", "免责声明", "RIR",
    ):
        assert rule in soul
    # Keep the persona a role/behavior/privacy/safety brief, not a second API manual.
    assert len(soul) <= 3200
    assert "## 只读路由与对话示例" not in soul
    assert "## 缺数据先查，再按服务器策略操作" not in soul


def test_persona_safety_is_conditional_and_preserves_privacy():
    soul = _read("deploy/hermes/SOUL.md")
    for rule in (
        "当前本人", "胸痛", "呼吸困难", "晕厥", "当地急救", "不等待工具",
        "没有红旗", "不附急救", "一般知识", "不得诊断", "设备", "风险",
        "凭据", "数据库", "私密配置", "影子档案", "现有模型 API",
        "不向其他外部服务传输", "明确要求记录", "成功", "反馈",
    ):
        assert rule in soul


def test_facts_arithmetic_and_coaching_judgment_have_explicit_owners():
    skill = _read("skills/vitalis/SKILL.md")
    coach = _read("skills/healthy-fitness-coach/SKILL.md")
    for rule in (
        "确定性工具", "单位换算", "格式化", "API 事实", "用户自述",
        "来源", "观测时间", "缺失不是零", "synthetic", "mock",
        "恢复分数", "生理", "已保存", "陈旧只限制依赖该快照的结论",
        "服务器推送继续使用确定性引擎", "不自动成为已接受计划",
    ):
        assert rule in skill
    for rule in (
        "knowledge-only", "Vitalis", "确定性工具", "完成", "组数", "次数", "休息",
        "记录负重", "起点", "调整", "已知疼痛", "热量目标", "补剂剂量",
    ):
        assert rule in coach


def test_plan_judgment_uses_records_and_checks_the_actual_calendar():
    coach = _read("skills/healthy-fitness-coach/SKILL.md")
    for rule in (
        "目标和频率", "完整", "已知动作", "记录负重", "可调整起点",
        "周末", "腿部", "长跑", "逐日检查", "前一天", "休息",
        "同一处", "RIR", "不重复询问", "临时停练", "长期习惯",
    ):
        assert rule in coach
    assert "不猜公斤数" not in coach


def test_completed_workout_review_answers_without_audit_or_rir_tail():
    coach = _read("skills/healthy-fitness-coach/SKILL.md")
    skill = _read("skills/vitalis/SKILL.md")
    for rule in (
        "默认采用记录中的动作名称和负重", "组间", "训练目标", "下次",
        "不把整场 RPE 当每组 RIR", "内部", "一句", "恢复",
    ):
        assert rule in coach + skill
    assert "已完成训练量" in coach
    assert "最大耐受量" in coach


def test_race_equivalence_is_distinct_from_a_personal_forecast():
    skill = _read("skills/vitalis/SKILL.md")
    for rule in (
        "情景等效", "实际预测", "确定性", "最近成绩", "目标距离",
        "轻松跑", "全力", "个人置信度", "未知", "不虚构",
        "race-equivalence --id", "--source", "--target-km", "GET /coach/race-equivalence",
        "scenario", "user_reported_maximal", "1.06", "1.04", "1.10", "敏感性",
        "elapsed", "duration", "不写入", "代表性",
    ):
        assert rule in skill


def test_running_snapshot_and_research_keep_their_qualifications():
    skill = _read("skills/vitalis/SKILL.md")
    for rule in (
        "query running --day", "--include-research", "纯快照", "state", "context", "trends",
        "近 28 天", "前 28 天", "不必追问窗口", "训练结构", "同类型", "用力",
        "心率", "配速", "耐力", "原始中位配速", "min:ss/km", "单位换算",
        "CS", "fixedHR", "durability", "shadow_only", "unvalidated",
        "不是正式评分", "不能说系统没有模型",
        "recorded_effort", "session_rpe", "classification_basis=inferred_not_user_goal",
        "比赛资格", "不让分类压过自述",
    ):
        assert rule in skill


def test_skill_loading_and_truthful_source_handling_are_owned_once():
    soul = _read("deploy/hermes/SOUL.md")
    skill = _read("skills/vitalis/SKILL.md")
    for rule in ("skills", "healthy-fitness-coach", "至少一份相关参考"):
        assert rule in soul
    for rule in ("一般知识", "无需个人 API", "不把合成来源说成设备实测"):
        assert rule in skill


@pytest.mark.parametrize("relative", ["skills/vitalis/SKILL.md", "deploy/hermes/SOUL.md"])
def test_no_unsupported_action_promises(relative):
    text = _read(relative)
    assert "同一轮" in text
    assert "结果" in text
    assert "已保存" in text


def test_persona_is_reusable_and_uses_hermes_python():
    soul = _read("deploy/hermes/SOUL.md")
    assert 'python "$HERMES_HOME/skills/vitalis/scripts/vitalis_api.py"' in soul
    assert "Hermes 虚拟环境" in soul
    assert "先通过 skills 工具读取 `vitalis`" in soul
    assert not re.search(r"\b20\d{2}-\d{2}-\d{2}\b|[A-Za-z]:\\|/opt/|/var/lib/", soul)
    assert "TELEGRAM_ALLOWED_USERS=" not in soul
    assert "VITALIS_ACCESS_TOKEN=" not in soul
