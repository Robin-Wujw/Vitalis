"""Zepp 数据源连接器（真实模式：apptoken 导入）。

Zepp（华米）真实授权方式：网页登录拿 apptoken，非 OAuth2 扫码。

接入步骤（真实数据）：
  1) 用户在官方网页 watchface.zepp.com（备用 user.huami.com）用账号密码登录
  2) 从登录 cookie `hm-user-login-info` 提取 user_id + apptoken
  3) 导入 Vitalis（POST /api/v1/connect/zepp/token）-> 验证 -> 保存 -> 同步

数据获取（对齐 ZeppBridge 已实测端点）：
  - 睡眠/活动：/v1/data/band_data.json（summary 为 base64 JSON）
  - 运动：/v1/sport/{sport}/history.json（13 种运动类型）
  - 训练负荷 / VO₂max：/v2/watch/users/{id}/WatchSportStatistics/{statistic}
  - HRV：/v2/users/me/events?eventType=hrv_sdnn
  - 每日摘要：/v2/users/me/events?eventType=DailyHealth

mock 模式保留：模拟 apptoken 同构数据 + 扫码演示，离线可端到端。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from vitalis.config import settings
from vitalis.models import AuthToken, MetricSample, NormalizedDaily, TrainingRecord, User
from vitalis.time import local_today

from ..base import ConnectorAuth, ConnectorSyncResult, HealthConnector
from ..registry import register_connector
from .client import MockOAuthToken, MockZeppClient, ZeppAPIClient, ZeppAuthError, SPORTS
from .fetcher import DataFetcher, FetchWindow
from .sync_manager import (
    DENSE_ARCHIVE_BATCH_SIZE,
    SyncManager,
    SyncReport,
    StreamReport,
)

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
            raise ZeppAuthError("apptoken 不能为空", kind="invalid_request")
        existing = repo.get_token(vitalis_user_id, self.source)
        if (
            existing is not None
            and existing.source_user_id
            and vendor_user_id
            and existing.source_user_id != vendor_user_id
        ):
            raise ZeppAuthError(
                "当前本地用户已绑定其他 Zepp 账号；请先断开并清理该用户数据",
                kind="invalid_request",
            )
        region = region_host.strip() or DEFAULT_REGION
        client = ZeppAPIClient(app_token=app_token, user_id=vendor_user_id or "me", region_host=region)
        # 验证：拉设备列表（或按需用 /v2/users/me/events 探测）
        try:
            client.verify()
        except ZeppAuthError as exc:
            raise ZeppAuthError(
                f"token 验证失败：{exc}。请确认 apptoken/user_id 与区域正确",
                kind=exc.kind,
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
            raise ZeppAuthError("Zepp 使用 apptoken 导入，不走扫码；请用 POST /connect/zepp/token 导入", kind="invalid_request")
        from .client import generate_state

        state = generate_state()
        return self._mock_client.get_authorize_url(state), state

    def authorize_url_for(self, state: str) -> str:
        if not self.mock:
            raise ZeppAuthError("Zepp 使用 apptoken 导入，不走扫码", kind="invalid_request")
        return self._mock_client.get_authorize_url(state)

    def exchange_and_save(self, repo, user_id: str, code: str, state: str = "") -> AuthToken:
        if not self.mock:
            raise ZeppAuthError("真实模式请用 POST /connect/zepp/token 导入 apptoken", kind="invalid_request")
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

    def create_attempt(
        self, user_id: str, *, days: int = 730, window: FetchWindow | None = None,
        trigger: str = "manual", trigger_ref: str | None = None,
        decode_dense_files: bool = False,
    ):
        """Create/reuse a durable attempt without doing network work."""
        from vitalis.services.zepp_sync_coordinator import ZeppSyncCoordinator

        coordinator = ZeppSyncCoordinator(
            connector=self,
            lease_seconds=getattr(settings, "sync_lease_seconds", 120),
            attempt_lease_seconds=getattr(settings, "sync_attempt_lease_seconds", 300),
        )
        return coordinator.create_attempt(
            user_id,
            days=days,
            window=window,
            trigger=trigger,
            trigger_ref=trigger_ref,
            timezone_name=settings.timezone,
            options={"decode_dense_files": decode_dense_files},
        )

    def sync_with_report(
        self, user: User, days: int = 730, repo=None,
        decode_dense_files: bool = False, max_chunks: int | None = None,
        window: FetchWindow | None = None, trigger: str = "manual",
        trigger_ref: str | None = None, attempt_id: str | None = None,
    ) -> SyncReport:
        """Coordinator facade; ``repo`` remains only for legacy mock callers."""
        if self.mock:
            if attempt_id is None:
                attempt = self.create_attempt(
                    user.id, days=days, window=window, trigger=trigger,
                    trigger_ref=trigger_ref, decode_dense_files=decode_dense_files,
                )
                attempt_id = attempt.id
            end = local_today()
            start = end - timedelta(days=min(days, 14) - 1)
            dailies = self._mock_fetch(user, start, end)
            if repo:
                for d in dailies:
                    repo.save_daily(d)
            progress = {"attempt_id": attempt_id, "status": "succeeded", "retry": 0}
            if attempt_id:
                from uuid import uuid4
                from vitalis.storage import HealthRepository, session_scope
                now = datetime.now(timezone.utc)
                with session_scope() as db:
                    ledger = HealthRepository(db)
                    attempt = ledger.sync_attempt(attempt_id)
                    if attempt and attempt.status in ("queued", "retry_wait"):
                        token = uuid4().hex
                        if ledger.claim_sync_attempt(attempt_id, token, now=now):
                            for chunk in ledger.sync_chunks(attempt_id):
                                if ledger.claim_sync_chunk(chunk.id, uuid4().hex, now=now):
                                    claimed = ledger.sync_chunk(attempt_id, chunk.stable_key)
                                    if claimed:
                                        ledger.finalize_chunk(
                                            claimed.id, claimed.lease_token, claimed.lease_epoch,
                                            "succeeded", now=now,
                                            stages={
                                                **dict(claimed.stages or {}),
                                                "fetch_status": "success",
                                                "parse_status": "success",
                                                "write_status": "success",
                                            },
                                        )
                            ledger.finalize_attempt(attempt_id, token, attempt.lease_epoch, "succeeded", now=now)
                    final = ledger.sync_attempt(attempt_id)
                    progress = {"attempt_id": attempt_id, "status": final.status if final else "succeeded", "retry": 0}
            return SyncReport(
                success=True,
                streams=[StreamReport(stream="mock", status="success", records_written=len(dailies))],
                records_written=len(dailies), progress=progress,
            )
        from vitalis.services.zepp_sync_coordinator import ZeppSyncCoordinator

        coordinator = ZeppSyncCoordinator(
            connector=self,
            lease_seconds=getattr(settings, "sync_lease_seconds", 120),
            attempt_lease_seconds=getattr(settings, "sync_attempt_lease_seconds", 300),
        )
        if attempt_id is None:
            attempt = coordinator.create_attempt(
                user.id,
                days=days,
                window=window,
                trigger=trigger,
                trigger_ref=trigger_ref,
                timezone_name=settings.timezone,
                options={"decode_dense_files": decode_dense_files},
            )
            attempt_id = attempt.id
        return coordinator.run_attempt(attempt_id, max_chunks=max_chunks)

    def fetch(
        self, user: User, start: date | None = None, end: date | None = None, repo=None
    ) -> list[NormalizedDaily]:
        end = end or local_today()
        start = start or (end - timedelta(days=14))
        if start > end:
            start, end = end, start
        if self.mock:
            return self._mock_fetch(user, start, end)
        # Real mode synchronizes first, then rebuilds using a separate read-only session.
        window = FetchWindow.local_dates(start, end)
        report = self.sync_with_report(user, window=window)
        if not report.success:
            raise ZeppAuthError(
                report.message or "Zepp 同步失败或数据不完整",
                kind=report.error_kind or "unknown",
                needs_reauth=report.needs_reauth,
            )
        from vitalis.storage import session_scope, HealthRepository
        with session_scope() as db:
            return self._rebuild_dailies(HealthRepository(db), user.id, start, end)

    def _mock_fetch(self, user: User, start: date, end: date) -> list[NormalizedDaily]:
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
        results: list[NormalizedDaily] = []
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
            metric_samples = []
            if day in hrv:
                metric_samples.append(MetricSample(
                    user_id=user.id,
                    metric="hrv_sdnn",
                    timestamp=datetime.combine(day, time.min, tzinfo=timezone.utc),
                    value=hrv[day],
                    unit="ms",
                    source_scope="user_fused",
                ))
            results.append(NormalizedDaily(
                user_id=user.id, date=day,
                sleep=sleeps.get(day),
                activity=activities.get(day),
                training=training,
                metric_samples=metric_samples,
            ))
            day += timedelta(days=1)
        return results

    def _rebuild_dailies(self, repo, user_id: str, start: date, end: date) -> list[NormalizedDaily]:
        """Rebuild normalized daily source records from storage."""
        from vitalis.models import ActivityRecord, SleepRecord, TrainingRecord
        out: list[NormalizedDaily] = []
        sleep_map = {date.fromisoformat(r["date"]): r for r in repo.sleep_range(user_id, start, end)}
        act_map = {date.fromisoformat(r["date"]): r for r in repo.activity_range(user_id, start, end)}
        train_map = {date.fromisoformat(r["date"]): r for r in repo.training_range(user_id, start, end)}
        day = start
        while day <= end:
            s = SleepRecord.model_validate(sleep_map[day]) if day in sleep_map else None
            a = ActivityRecord.model_validate(act_map[day]) if day in act_map else None
            t = TrainingRecord.model_validate(train_map[day]) if day in train_map else None
            out.append(NormalizedDaily(user_id=user_id, date=day, sleep=s, activity=a, training=t))
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
