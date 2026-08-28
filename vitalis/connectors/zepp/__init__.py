"""Zepp 数据源连接器（真实模式：apptoken 导入）。

Zepp（华米）真实授权方式：网页登录拿 apptoken，非 OAuth2 扫码。

接入步骤（真实数据）：
  1) 用户在官方网页 watchface.zepp.com（备用 user.huami.com）用账号密码登录
  2) 从登录 cookie `hm-user-login-info` 提取 user_id + apptoken
  3) 导入 Vitalis（POST /api/v1/connect/zepp/token）-> 验证 -> 保存 -> 同步

数据获取（对齐 ZeppBridge 已实测端点）：
  - 睡眠/活动：/v1/data/band_data.json（summary 为 base64 JSON）
  - 运动：/v1/sport/{sport}/history.json（13 种运动类型）
  - 训练负荷：/v2/watch/users/{id}/WatchSportStatistics/SPORT_LOAD
  - HRV：/v2/users/me/events?eventType=hrv_sdnn
  - 每日摘要：/v2/users/me/events?eventType=DailyHealth

mock 模式保留：模拟 apptoken 同构数据 + 扫码演示，离线可端到端。
"""
from __future__ import annotations

from datetime import date, timedelta

from vitalis.config import settings
from vitalis.models import AuthToken, DailyHealth, TrainingRecord, User

from ..base import ConnectorAuth, ConnectorSyncResult, HealthConnector
from ..registry import register_connector
from .client import MockOAuthToken, MockZeppClient, ZeppAPIClient, ZeppAuthError, SPORTS
from .fetcher import DataFetcher, FetchWindow
from .sync_manager import SyncManager, SyncReport, StreamReport

DEFAULT_REGION = "api-mifitcn.zepp.com"  # 中国区缺省（其它区按账号区域）


class AuthRequired(RuntimeError):
    """尚未导入 apptoken：数据获取前需完成凭证导入。"""


@register_connector
class ZeppConnector(HealthConnector):
    source = "zepp"

    def __init__(self, auth: ConnectorAuth | None = None, mock: bool | None = None):
        super().__init__(auth)
        self.mock = settings.zepp_mock if mock is None else mock
        self._mock_client = MockZeppClient() if self.mock else None
        from .parser import ZeppParser

        self.parser = ZeppParser()

    # ---------------- apptoken 导入（真实接入主路径） ----------------

    def import_token(self, repo, vitalis_user_id: str, app_token: str,
                     vendor_user_id: str = "", region_host: str = "") -> AuthToken:
        """验证并保存 apptoken 凭据。

        args:
            repo: 存储仓储（保存 token）
            vitalis_user_id: 系统内用户 id
            app_token: Zepp apptoken（来自登录 cookie hm-user-login-info）
            vendor_user_id: Zepp 用户 id（可选，未知时用 "me" 探测）
            region_host: 区域主机（缺省中国区 api-mifitcn.zepp.com）
        """
        if not app_token:
            raise ZeppAuthError("apptoken 不能为空")
        region = region_host.strip() or DEFAULT_REGION
        client = ZeppAPIClient(app_token=app_token, user_id=vendor_user_id or "me", region_host=region)
        # 验证：拉设备列表（或按需用 /v2/users/me/events 探测）
        try:
            client.verify()
        except ZeppAuthError as exc:
            raise ZeppAuthError(
                f"token 验证失败：{exc}。请确认 apptoken/user_id 与区域正确",
                needs_reauth=exc.needs_reauth,
            )

        auth = AuthToken(
            user_id=vitalis_user_id,
            source=self.source,
            access_token=app_token,
            scope="apptoken",
            region_host=region,
            source_user_id=vendor_user_id or None,
        )
        repo.save_token(auth)
        return auth

    def load_token(self, repo, user_id: str) -> AuthToken | None:
        return repo.get_token(user_id, self.source)

    # ---------------- 扫码演示（仅 mock） ----------------

    def authorize_url(self) -> tuple[str, str]:
        if not self.mock:
            raise ZeppAuthError("Zepp 使用 apptoken 导入，不走扫码；请用 POST /connect/zepp/token 导入")
        from .client import generate_state

        state = generate_state()
        return self._mock_client.get_authorize_url(state), state

    def authorize_url_for(self, state: str) -> str:
        if not self.mock:
            raise ZeppAuthError("Zepp 使用 apptoken 导入，不走扫码")
        return self._mock_client.get_authorize_url(state)

    def exchange_and_save(self, repo, user_id: str, code: str, state: str = "") -> AuthToken:
        if not self.mock:
            raise ZeppAuthError("真实模式请用 POST /connect/zepp/token 导入 apptoken")
        token: MockOAuthToken = self._mock_client.exchange_code(code)
        auth = AuthToken(
            user_id=user_id,
            source=self.source,
            access_token=token.access_token,
            refresh_token=token.refresh_token,
            expires_at=token.expires_at.replace(tzinfo=None) if token.expires_at else None,
            scope=token.scope,
            region_host="",
            source_user_id=token.source_user_id,
        )
        repo.save_token(auth)
        return auth

    # ---------------- 数据获取（新版：对齐 ZeppBridge SyncManager） ----------------

    def sync(
        self, user: User, start: date | None = None, end: date | None = None, repo=None
    ) -> ConnectorSyncResult:
        days = self.fetch(user, start, end, repo=repo)
        sleep_n = sum(1 for d in days if d.sleep)
        act_n = sum(1 for d in days if d.activity)
        wo_n = sum((d.training.workout_count or 0) for d in days if d.training)
        return ConnectorSyncResult(user.id, self.source, len(days), sleep_n, wo_n, act_n)

    def sync_with_report(
        self, user: User, days: int = 730, repo=None,
        on_progress=None,
    ) -> SyncReport:
        """完整同步并返回逐流报告（支持最长 730 天 / 2 年）。"""
        if self.mock:
            # mock 模式走简化逻辑
            end = date.today()
            start = end - timedelta(days=min(days, 14))
            dailies = self._mock_fetch(user, start, end)
            if repo:
                for d in dailies:
                    repo.save_daily(d)
            return SyncReport(
                success=True,
                streams=[StreamReport(stream="mock", status="success", records_written=len(dailies))],
                records_written=len(dailies),
            )
        client = self._client_for(repo, user)
        fetcher = DataFetcher(client)
        manager = SyncManager(fetcher)
        return manager.sync_report(user, days, repo=repo, on_progress=on_progress)

    def fetch(
        self, user: User, start: date | None = None, end: date | None = None, repo=None
    ) -> list[DailyHealth]:
        end = end or date.today()
        start = start or (end - timedelta(days=14))
        if start > end:
            start, end = end, start
        if self.mock:
            return self._mock_fetch(user, start, end)
        if repo is None:
            raise AuthRequired("fetch 真实数据需要提供 repo 会话")
        # 真实模式：先同步到库，再从库重建 DailyHealth 列表
        days = (end - start).days + 1
        self.sync_with_report(user, days=days, repo=repo)
        return self._rebuild_dailies(repo, user.id, start, end)

    def _mock_fetch(self, user: User, start: date, end: date) -> list[DailyHealth]:
        """mock 模式保持原有逻辑（确定性模拟数据）。"""
        client = self._mock_client
        band = client.fetch_band_data(start.isoformat(), end.isoformat(), "detail", 8, 0)
        sleeps, activities = self.parser.parse_band(band)
        workouts = []
        for sport in SPORTS:
            payload = client.fetch_sport_history(sport, 0, 9999999999, 1)
            workouts.extend(self.parser.parse_sport_history(payload, sport_hint=sport))
        hrv_raw = client.fetch_events("hrv_sdnn", "real_data", 0, 9999999999999, 2000, True)
        hrv = self.parser.parse_hrv_events(hrv_raw)
        results: list[DailyHealth] = []
        day = start
        while day <= end:
            day_workouts = [w for w in workouts if w.started_at and w.started_at.date() == day]
            training = None
            if day_workouts:
                training = TrainingRecord(
                    user_id=user.id, date=day,
                    workout_count=len(day_workouts),
                    total_duration=sum(w.duration for w in day_workouts),
                    total_load=sum(w.load for w in day_workouts),
                )
            results.append(DailyHealth(
                user_id=user.id, date=day,
                sleep=sleeps.get(day),
                activity=activities.get(day),
                training=training,
                hrv=hrv.get(day),
            ))
            day += timedelta(days=1)
        return results

    def _rebuild_dailies(self, repo, user_id: str, start: date, end: date) -> list[DailyHealth]:
        """从存储重建指定日期范围的 DailyHealth 列表。"""
        from vitalis.models import ActivityRecord, SleepRecord, TrainingRecord
        out: list[DailyHealth] = []
        sleep_map = {date.fromisoformat(r["date"]): r for r in repo.sleep_range(user_id, start, end)}
        act_map = {date.fromisoformat(r["date"]): r for r in repo.activity_range(user_id, start, end)}
        train_map = {date.fromisoformat(r["date"]): r for r in repo.training_range(user_id, start, end)}
        day = start
        while day <= end:
            s = SleepRecord.model_validate(sleep_map[day]) if day in sleep_map else None
            a = ActivityRecord.model_validate(act_map[day]) if day in act_map else None
            t = TrainingRecord.model_validate(train_map[day]) if day in train_map else None
            hd = repo.health_daily(user_id, day)
            daily = DailyHealth(user_id=user_id, date=day, sleep=s, activity=a, training=t)
            if hd:
                daily.hrv = hd.hrv
                daily.recovery_score = hd.recovery_score
                daily.recovery_level = hd.recovery_level
                daily.stress_level = hd.stress_level
                daily.overall_score = hd.overall_score
            out.append(daily)
            day += timedelta(days=1)
        return out

    def _client_for(self, repo, user: User):
        if self.mock:
            return self._mock_client
        if repo is None:
            raise AuthRequired("缺少存储会话，无法读取凭据")
        auth = self.load_token(repo, user.id)
        if auth is None:
            raise AuthRequired(
                f"用户 {user.id} 尚未导入 Zepp 凭据。"
                "登录 watchface.zepp.com 后从 cookie hm-user-login-info 取 user_id+apptoken，"
                "POST /api/v1/connect/zepp/token 导入"
            )
        vendor_id = auth.source_user_id or user.source_user_id or ""
        if vendor_id:
            user.source_user_id = vendor_id
        return ZeppAPIClient(
            app_token=auth.access_token,
            user_id=vendor_id or "me",
            region_host=auth.region_host or DEFAULT_REGION,
        )

    # ---- 兼容基类（非扫码场景用配置 token 直连） ----
    def authenticate(self) -> ConnectorAuth:
        if self.mock:
            self.auth = ConnectorAuth(token="mock-token", extra={"mode": "mock"})
        else:
            self.auth = ConnectorAuth(token=settings.zepp_access_token, extra={"mode": "apptoken"})
        return self.auth
