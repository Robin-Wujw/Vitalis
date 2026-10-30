"""Static behavior-policy and executable CLI contracts, not an LLM evaluation.

The prose assertions prevent contradictory policy artifacts from shipping. They
cannot establish that a model follows the policy. CLI tests execute only the real
client's read routing against a synthetic request function; candidate multi-turn
Hermes acceptance is a separate deployment gate documented in docs/agents.md.
"""
from pathlib import Path
import runpy

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "vitalis"


def _read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


@pytest.mark.parametrize("relative", [
    "deploy/hermes/SOUL.md",
    "skills/vitalis/SKILL.md",
    "skills/healthy-fitness-coach/SKILL.md",
    "skills/healthy-fitness-coach/references/training-principles.md",
    "skills/healthy-fitness-coach/references/technique-and-session-education.md",
    "docs/agents.md",
])
def test_policy_does_not_blanket_ban_grounded_arithmetic_or_planning(relative):
    text = _read(relative)
    # Regression checks for rules that previously overrode task completion.
    for obsolete in (
        "不得自行计算个人趋势、恢复分数、相关、训练容量或其他派生分析",
        "不得自行计算个人恢复分数、训练容量或其他派生分析",
        "不得自行推导个人指标",
        "不另算容量", "不是另算容量", "没有单位或计重方式时不计算容量",
        "不编译周计划、不生成日程",
        "不能用用户上次重量自行算出今天该举多少",
        "本包不根据用户上次重量推算新重量",
        "不计算个人指标或执行工具与写入",
    ):
        assert obsolete not in text, f"{relative}: obsolete blanket rule: {obsolete}"


def test_gym_capacity_routes_to_completed_nominal_volume_before_clarification():
    text = _read("skills/vitalis/SKILL.md")
    section = text.split("## 已完成力量训练与容量", 1)[1].split("\n## ", 1)[0]
    for rule in (
        "默认指已完成训练量", "不是最大耐受量", "先计算已知部分",
        "strength-review --from", "--to", "--limit 100", "GET /coach/strength-review",
        "read", "application/intelligence", "不写入", "名义", "重量 × 次数",
        "未知计重方式", "分组", "自重", "次数", "不能记为零",
        "可比", "此前", "生理容量", "不把整场 RPE 当每组 RIR",
    ):
        assert rule in section


def test_api_skill_owns_request_permissions_without_persona_duplication():
    text = _read("skills/vitalis/SKILL.md")
    for rule in (
        "coach-policy", "GET /coach/policy", "enabled", "expires_at", "revision",
        "allow_refresh", "allow_analyze", "默认拒绝", "服务器拥有",
        "coach-request refresh-workouts --day", "coach-request analyze --day",
        "read+coach+feedback", "3 次/小时", "1 个 inflight", "28 天", "4 条",
        "最多 2 个根操作", "最多 3 次", "403", "429", "同一 key",
        "CPU", "子任务", "新请求受理", "不取消", "不反复征求",
        "enqueue", "rearm", "repoint", "独立定时", "正常分析",
        "今天或昨天", "源数据", "陈旧或缺失的已保存分析", "未来观察", "失败模型",
        "不回退", "references/api.md", "相关操作", "schema", "不整份",
    ):
        assert rule in text
    assert "--authorized" not in text
    soul = _read("deploy/hermes/SOUL.md")
    for contract_detail in ("GET /coach/policy", "max_requests_per_hour", "rearm", "--limit 500"):
        assert contract_detail not in soul
    integration = _read("docs/agents.md")
    assert "90 天" in integration
    assert '"max_lookback_days":28' in integration


def test_read_windows_and_detail_limits_are_not_complete_history_claims():
    text = _read("skills/vitalis/SKILL.md")
    for rule in (
        "月初", "截止日", "workouts --from", "--limit 500",
        "--strength-details --limit 20", "detail.strength_sets", "--samples",
        "strength_details.state", "一次缺失不能推广为全部缺失", "截断",
        "同一天仍达到上限", "不能据此证明同步覆盖完整", "不是完整月报",
        "完整 `report daily --day", "query explain --day", "标题",
    ):
        assert rule in text


def test_policy_verification_is_not_claimed_as_model_evaluation():
    text = _read("docs/agents.md")
    for rule in (
        "静态", "不证明 LLM", "真实模型多轮评估", "工具调用", "隔离 home",
        "容量", "最大耐受", "同一轮", "腿部", "长跑", "安全问题",
        "情景等效", "预测", "历史验收不能替代本次候选验收",
    ):
        assert rule in text


@pytest.mark.parametrize(("argv", "path", "params"), [
    (["workouts", "--from", "2026-02-01", "--to", "2026-02-18", "--limit", "500"],
     "workouts", {"from": "2026-02-01", "to": "2026-02-18", "limit": 500}),
    (["workouts", "--id", "synthetic-session", "--source", "mock"],
     "workouts/synthetic-session", {"source": "mock", "include_samples": "false"}),
    (["workouts", "--from", "2026-02-01", "--to", "2026-02-18",
      "--strength-details", "--limit", "20"],
     "workouts", {"from": "2026-02-01", "to": "2026-02-18", "limit": 20,
                  "include_strength_details": "true"}),
    (["strength-review", "--from", "2026-02-01", "--to", "2026-02-18", "--limit", "100"],
     "coach/strength-review", {"from": "2026-02-01", "to": "2026-02-18", "limit": 100}),
    (["race-equivalence", "--id", "synthetic-run", "--source", "mock",
      "--target-km", "21.0975"],
     "coach/race-equivalence", {"workout_id": "synthetic-run", "source": "mock",
                                 "target_km": 21.0975, "effort_basis": "scenario"}),
    (["race-equivalence", "--id", "synthetic-run", "--source", "mock",
      "--target-km", "10", "--effort-basis", "user_reported_maximal"],
     "coach/race-equivalence", {"workout_id": "synthetic-run", "source": "mock",
                                 "target_km": 10.0, "effort_basis": "user_reported_maximal"}),
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


@pytest.mark.parametrize("limit", ("0", "101"))
def test_strength_review_rejects_out_of_bound_limits_before_request(monkeypatch, limit):
    client = runpy.run_path(str(SKILL / "scripts" / "vitalis_api.py"))
    calls = []
    monkeypatch.setitem(client["run"].__globals__, "request", lambda *a, **kw: calls.append(a))
    with pytest.raises(client["ClientError"]) as error:
        client["parse_args"]([
            "strength-review", "--from", "2026-02-01", "--to", "2026-02-18", "--limit", limit,
        ])
    assert error.value.code == "invalid_arguments"
    assert calls == []
