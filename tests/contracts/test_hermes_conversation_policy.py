"""Static conversation contract and read-route tests, not an LLM evaluation.

Synthetic command traces ensure the documented coaching read path never writes.
Real multi-turn model behavior must still be accepted in an isolated Hermes home.
"""
from pathlib import Path
import runpy

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "vitalis"


def test_conversation_permissions_are_separate_from_server_reports():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert "## Hermes 对话与服务器推送" in text
    for phrase in (
        "服务器推送继续使用确定性引擎",
        "可以提出训练草案",
        "组次范围、休息和余力",
        "本次会话",
        "不自动成为已接受计划",
        "不要自行计算趋势",
        "不得诊断疾病",
        "陈旧只限制依赖该快照的结论",
        "不猜公斤数", "不把缺口当成已满足的安全条件",
        "默认是只读讨论", "不能假装已保存草案",
    ):
        assert phrase in text
    assert "只复述 API 返回" not in text
    assert "不提供推断训练决策" not in text


@pytest.mark.parametrize("relative", ["skills/vitalis/SKILL.md", "deploy/hermes/SOUL.md"])
def test_completed_workout_review_is_actionable_not_an_internal_audit(relative):
    text = (ROOT / relative).read_text(encoding="utf-8")
    for phrase in (
        "已完成训练评价", "默认采用记录中的动作名称和负重",
        "不主动输出", "观察窗口", "组间", "训练目标",
        "定性比较", "不把整场 RPE 当每组 RIR",
        "不自动提升", "补取", "不默认追加", "下次", "结构", "免责声明",
    ):
        assert phrase in text


@pytest.mark.parametrize("relative", ["skills/vitalis/SKILL.md", "deploy/hermes/SOUL.md"])
def test_refresh_permission_is_scoped_and_side_effects_are_not_hidden(relative):
    text = (ROOT / relative).read_text(encoding="utf-8")
    for phrase in (
        "refresh-workouts --day", "--authorized --key-file",
        "最多", "4 条", "后台分析", "待投递报告",
        "每", "一次", "长期授权", "用户控制",
        "不是", "授权", "只读", "观察窗口",
    ):
        assert phrase in text


def test_reading_routes_preserve_window_and_complete_plan_guidance():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    for phrase in (
        "workouts --from", "--to", "月初", "截止日",
        "query training-preferences", "query explain --day",
        "context", "标题", "截断", "不是完整月报",
        "临时", "长期", "重复追问", "--strength-details --limit 20",
        "同一天仍达到上限", "不能据此证明同步覆盖完整",
    ):
        assert phrase in text


@pytest.mark.parametrize(("argv", "path", "params"), [
    (["workouts", "--from", "2026-02-01", "--to", "2026-02-18", "--limit", "500"],
     "workouts", {"from": "2026-02-01", "to": "2026-02-18", "limit": 500}),
    (["workouts", "--id", "synthetic-session", "--source", "mock"],
     "workouts/synthetic-session", {"source": "mock", "include_samples": "false"}),
    (["workouts", "--from", "2026-02-01", "--to", "2026-02-18",
      "--strength-details", "--limit", "20"],
     "workouts", {"from": "2026-02-01", "to": "2026-02-18", "limit": 20,
                  "include_strength_details": "true"}),
    (["query", "training-preferences"], "intelligence/training-preferences", None),
    (["query", "explain", "--day", "2026-02-18"],
     "intelligence/explain", {"day": "2026-02-18"}),
    (["report", "daily", "--day", "2026-02-18", "--state"],
     "reports/daily/state", {"day": "2026-02-18"}),
])
def test_coaching_queries_only_read(monkeypatch, argv, path, params):
    client = runpy.run_path(str(SKILL / "scripts" / "vitalis_api.py"))
    monkeypatch.setenv("VITALIS_API_BASE_URL", "http://127.0.0.1:9000")
    monkeypatch.setenv("VITALIS_ACCESS_TOKEN", "synthetic-read-token")
    calls = []

    def request(method, requested_path, token, **kwargs):
        calls.append((method, requested_path, kwargs))
        return {"synthetic": True}

    monkeypatch.setitem(client["run"].__globals__, "request", request)
    result = client["run"](client["parse_args"](argv))
    assert result == {"synthetic": True}
    assert calls == [("GET", path, {"params": params})]
