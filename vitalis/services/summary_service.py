"""汇总服务：查询存储 -> 组装 DailyHealth -> 分析流水线 -> 返回结果。"""

from datetime import date, timedelta

from vitalis.analysis import AnalysisPipeline
from vitalis.models import ActivityRecord, DailyHealth, SleepRecord, TrainingRecord
from vitalis.storage import HealthRepository, session_scope
from vitalis.storage.models import AnalysisRecord as OrmAnalysis


class SummaryService:
    """每日健康汇总 + 分析。"""

    HISTORY_DAYS = 30  # 统计引擎需要的历史窗口

    def __init__(self, pipeline: AnalysisPipeline | None = None):
        self.pipeline = pipeline or AnalysisPipeline()

    def today(self, user_id: str, day: date | None = None) -> dict:
        """获取某日健康状态（对应 API GET /health/today）。"""
        day = day or date.today()
        with session_scope() as db:
            repo = HealthRepository(db)
            target = self._build_daily(repo, user_id, day)
            if target is None:
                return {"user_id": user_id, "date": day.isoformat(), "found": False}

            history = self._history(repo, user_id, day)
            result = self.pipeline.run(target, history)

            # 持久化快照与分析记录
            repo.save_daily(target)
            repo.add_analysis(OrmAnalysis(
                id=f"an-{user_id}-{day.isoformat()}",
                user_id=user_id,
                engine="pipeline",
                kind="daily_summary",
                inputs={"date": day.isoformat()},
                output=result.explanation,
                score=result.decision.overall_score,
            ))

        payload = result.to_dict()
        payload.update({
            "user_id": user_id,
            "date": day.isoformat(),
            "found": True,
            # 未配置 LLM 时说明是模板生成
            "engine": "rule+statistical" + ("+ai" if result.llm_used else " (template)"),
        })
        return payload

    def analyze(self, user_id: str, day: date | None = None) -> dict:
        """POST /analyze 的完整分析（含当日各维度明细）。"""
        payload = self.today(user_id, day)
        with session_scope() as db:
            repo = HealthRepository(db)
            day = date.fromisoformat(payload["date"])
            daily = self._build_daily(repo, user_id, day)
        if daily:
            payload["sleep"] = daily.sleep.model_dump(mode="json") if daily.sleep else None
            payload["activity"] = daily.activity.model_dump(mode="json") if daily.activity else None
            payload["training"] = daily.training.model_dump(mode="json") if daily.training else None
        return payload

    # ---- 存储 -> 模型 ----
    @staticmethod
    def _build_daily(repo: HealthRepository, user_id: str, day: date) -> DailyHealth | None:
        sleep_raw = repo.get_sleep(user_id, day)
        activity_raw = repo.activity_range(user_id, day, day)
        training_raw = repo.training_range(user_id, day, day)

        sleep = SleepRecord.model_validate(sleep_raw) if sleep_raw else None
        activity = ActivityRecord.model_validate(activity_raw[0]) if activity_raw else None
        training = TrainingRecord.model_validate(training_raw[0]) if training_raw else None

        if not sleep and not activity and not training:
            return None
        daily = DailyHealth(user_id=user_id, date=day, sleep=sleep, activity=activity, training=training)

        # 读取已持久化的分析列（hrv/趋势/分）
        hd = repo.health_daily(user_id, day)
        if hd:
            daily.hrv = hd.hrv
            daily.hrv_trend_pct = hd.hrv_trend_pct
            daily.recovery_score = hd.recovery_score
            daily.recovery_level = hd.recovery_level or "moderate"
            daily.stress_level = hd.stress_level or "medium"
            daily.overall_score = hd.overall_score
            daily.summary = hd.summary
        return daily

    @staticmethod
    def _history(repo: HealthRepository, user_id: str, day: date) -> list[DailyHealth]:
        """取 target 日之前最近 N 天的历史快照（用于趋势）。"""
        start = day - timedelta(days=SummaryService.HISTORY_DAYS)
        out: list[DailyHealth] = []
        cur = start
        while cur < day:
            d = SummaryService._build_daily(repo, user_id, cur)
            if d:
                out.append(d)
            cur += timedelta(days=1)
        return out