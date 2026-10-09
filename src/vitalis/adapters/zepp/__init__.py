"""Zepp 数据源连接器（真实模式：apptoken 导入）。

Zepp（华米）真实授权方式：网页登录拿 apptoken，非 OAuth2 扫码。

接入步骤（真实数据）：
  1) 用户在官方网页 watchface.zepp.com（备用 user.huami.com）用账号密码登录
  2) 从登录 cookie `hm-user-login-info` 提取 user_id + apptoken
  3) 导入 Vitalis（POST /api/connect/zepp/token）-> 验证 -> 保存 -> 同步

数据获取（对齐 ZeppBridge 已实测端点）：
  - 睡眠/活动：/v1/data/band_data.json（summary 为 base64 JSON）
  - 运动：/v1/sport/run/history.json（账号内混合运动记录）
  - 训练负荷 / VO₂max：/v2/watch/users/{id}/WatchSportStatistics/{statistic}
  - HRV：/v2/users/me/events?eventType=hrv_sdnn
  - 每日摘要：/v2/users/me/events?eventType=DailyHealth

mock 模式保留：模拟 apptoken 同构数据 + 扫码演示，离线可端到端。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from vitalis.config import settings
from vitalis.domain import AuthToken, MetricSample, NormalizedDaily, TrainingRecord, User
from vitalis.adapters.persistence.repositories import SourceIdentityConflict
from vitalis.time import local_today

from vitalis.application.connector import ConnectorAuth, ConnectorSyncResult, HealthConnector
from vitalis.application.ports import CredentialInput
from .auth_parser import extract_from_login_info
from .client import MockOAuthToken, MockZeppClient, ZeppAPIClient, ZeppAuthError, SPORTS
from .fetcher import MAX_SYNC_DAYS, DataFetcher, FetchWindow
from .sync_manager import (
    DENSE_ARCHIVE_BATCH_SIZE,
    SyncManager,
    SyncReport,
    StreamReport,
)

DEFAULT_REGION = "api-mifitcn.zepp.com"  # 中国区缺省（其它区按账号区域）

__all__ = [
    "AuthRequired", "ZeppConnector", "MAX_SYNC_DAYS", "DataFetcher", "FetchWindow",
    "DENSE_ARCHIVE_BATCH_SIZE", "SyncManager", "SyncReport", "StreamReport",
]


class AuthRequired(RuntimeError):
    """尚未导入 apptoken：数据获取前需完成凭证导入。"""


class ZeppConnector(HealthConnector):
    source = "zepp"

    def __init__(self, auth: ConnectorAuth | None = None, mock: bool | None = None):
        super().__init__(auth)
        self.mock = settings.zepp_mock if mock is None else mock
        if self.mock and settings.env not in {"dev", "test"}:
            raise ValueError("ZEPP_MOCK cannot be enabled in production")
        self.source_mode = "mock" if self.mock else "real"
        self._mock_client = MockZeppClient(timezone_name=settings.timezone) if self.mock else None
        from .parser import ZeppParser

        self.parser = ZeppParser()

    @staticmethod
    def _close_client(client) -> None:
        """Close one-shot clients without requiring test doubles to implement it."""
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                # Cleanup must not turn a completed verification into a failure.
                pass

    # ---------------- credential provider boundary ----------------

    def parse_cookie(self, raw: str) -> CredentialInput | None:
        extracted = extract_from_login_info(raw)
        if extracted is None:
            return None
        return CredentialInput(
            vendor_user_id=extracted.user_id,
            app_token=extracted.app_token,
            region_hint=extracted.region_hint,
        )

    def probe_region_hosts(
        self,
        user_id: str,
        app_token: str,
        region_hint: str | None = None,
        saved_host: str | None = None,
    ) -> str:
        """Probe allowlisted Zepp hosts without involving persistence or HTTP routes."""
        from vitalis.adapters.zepp.auth_parser import preferred_region_hosts
        import concurrent.futures
        import threading

        hosts = preferred_region_hosts(saved_host, region_hint)
        if not hosts:
            hosts = ["https://api-mifitcn.zepp.com"]
        result: dict[str, str | None] = {"host": None}
        lock = threading.Lock()

        def try_host(host: str) -> None:
            if result["host"]:
                return
            client = None
            try:
                client = ZeppAPIClient(
                    app_token=app_token, user_id=user_id, region_host=host
                )
                client.verify()
                with lock:
                    if result["host"] is None:
                        result["host"] = client.region_host
            except Exception:
                pass
            finally:
                self._close_client(client)

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=min(len(hosts), 6))
        futures = []
        try:
            for host in hosts:
                futures.append(executor.submit(try_host, host))
            for _future in concurrent.futures.as_completed(futures, timeout=45):
                if result["host"]:
                    break
        except concurrent.futures.TimeoutError as exc:
            raise ZeppAuthError(
                "Zepp 区域主机探测超时，请稍后重试", kind="timeout"
            ) from exc
        finally:
            for future in futures:
                future.cancel()
            # Wait for already-running probes so every transport is closed before
            # returning or raising; cancelled futures need no cleanup.
            executor.shutdown(wait=True, cancel_futures=True)
        if result["host"] is None:
            raise ZeppAuthError(
                f"未能在允许的区域主机上验证账号。尝试了 {len(hosts)} 个主机，"
                "请确认已登录 watchface.zepp.com 且凭据未过期。",
                kind="vendor_response",
            )
        return result["host"]

    def verify_credentials(
        self,
        user_id: str,
        credentials: CredentialInput,
        *,
        saved_host: str | None = None,
    ) -> AuthToken:
        """Probe and verify vendor credentials outside the caller's write transaction."""
        if not credentials.vendor_user_id or not credentials.app_token:
            raise ZeppAuthError("apptoken/user_id 不能为空", kind="invalid_request")
        if self.mock:
            return AuthToken(
                user_id=user_id,
                source=self.source,
                access_token=credentials.app_token,
                scope="apptoken",
                region_host=credentials.region_hint or "",
                source_user_id=credentials.vendor_user_id,
            )
        region = self.probe_region_hosts(
            credentials.vendor_user_id,
            credentials.app_token,
            credentials.region_hint,
            saved_host,
        )
        client = ZeppAPIClient(
            app_token=credentials.app_token,
            user_id=credentials.vendor_user_id,
            region_host=region,
        )
        try:
            client.verify()
        finally:
            self._close_client(client)
        return AuthToken(
            user_id=user_id,
            source=self.source,
            access_token=credentials.app_token,
            scope="apptoken",
            region_host=region,
            source_user_id=credentials.vendor_user_id,
        )

    def exchange_code(self, user_id: str, code: str, state: str = "") -> AuthToken:
        return self.exchange(user_id, code, state)

    def verify_saved(self, token: AuthToken) -> None:
        if self.mock:
            return
        client = ZeppAPIClient(
            app_token=token.access_token,
            user_id=token.source_user_id or "",
            region_host=token.region_host,
        )
        try:
            client.verify()
        finally:
            self._close_client(client)

    # ---------------- apptoken 导入（真实接入主路径） ----------------

    def validate_token(self, repo, vitalis_user_id: str, app_token: str,
                       vendor_user_id: str = "", region_host: str = "") -> AuthToken:
        """Validate an apptoken without writing credentials or recreating a user."""
        if not app_token:
            raise ZeppAuthError("apptoken 不能为空", kind="invalid_request")
        vendor_user_id = vendor_user_id.strip()
        if not vendor_user_id:
            raise ZeppAuthError(
                "Zepp user_id 不能为空；请重新读取 hm-user-login-info cookie",
                kind="invalid_request",
            )
        existing = repo.get_token(vitalis_user_id, self.source)
        if (
            existing is not None
            and existing.source_user_id
            and vendor_user_id
            and existing.source_user_id != vendor_user_id
        ):
            raise ZeppAuthError(
                "当前本地用户已绑定其他 Zepp 账号；请先断开并清理该用户数据",
                kind="identity_conflict",
            )
        if repo.source_identity_owned_by_other(
            vitalis_user_id, self.source, vendor_user_id
        ):
            raise ZeppAuthError(
                "该 Zepp 账号已绑定到其他本地用户",
                kind="identity_conflict",
            )
        region = region_host.strip() or DEFAULT_REGION
        client = ZeppAPIClient(app_token=app_token, user_id=vendor_user_id, region_host=region)
        try:
            client.verify()
        except ZeppAuthError as exc:
            raise ZeppAuthError(
                f"token 验证失败：{exc}。请确认 apptoken/user_id 与区域正确",
                kind=exc.kind,
            )
        finally:
            self._close_client(client)
        return AuthToken(
            user_id=vitalis_user_id,
            source=self.source,
            access_token=app_token,
            scope="apptoken",
            region_host=region,
            source_user_id=vendor_user_id,
        )

    def import_token(self, repo, vitalis_user_id: str, app_token: str,
                     vendor_user_id: str = "", region_host: str = "") -> AuthToken:
        """Validate and persist an apptoken in the caller's transaction."""
        auth = self.validate_token(
            repo, vitalis_user_id, app_token, vendor_user_id, region_host
        )
        try:
            repo.save_token(auth)
        except SourceIdentityConflict as exc:
            raise ZeppAuthError(str(exc), kind="identity_conflict") from exc
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

    def exchange(self, user_id: str, code: str, state: str = "") -> AuthToken:
        if not self.mock:
            raise ZeppAuthError("真实模式请用 POST /connect/zepp/token 导入 apptoken", kind="invalid_request")
        token: MockOAuthToken = self._mock_client.exchange_code(code)
        return AuthToken(
            user_id=user_id,
            source=self.source,
            access_token=token.access_token,
            refresh_token=token.refresh_token,
            expires_at=token.expires_at.replace(tzinfo=None) if token.expires_at else None,
            scope=token.scope,
            region_host="",
            source_user_id=f"mock-user-{user_id}",
        )

    def exchange_and_save(self, repo, user_id: str, code: str, state: str = "") -> AuthToken:
        auth = self.exchange(user_id, code, state)
        try:
            repo.save_token(auth)
        except SourceIdentityConflict as exc:
            raise ZeppAuthError(str(exc), kind="identity_conflict") from exc
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
        decode_dense_files: bool = False, detail_backfill: bool = False,
        workout_only: bool = False, detail_only: bool = False,
        detail_refresh_before: str | None = None,
        detail_limit: int | None = None,
        repository=None,
    ):
        """Create/reuse a durable attempt without doing network work."""
        if (detail_backfill or workout_only or detail_only) and trigger != "manual":
            raise ValueError("historical workout options require a manual sync")
        if detail_refresh_before is not None and not detail_only:
            raise ValueError("detail_refresh_before requires detail_only")
        if detail_limit is not None and not detail_only:
            raise ValueError("detail_limit requires detail_only")
        from vitalis.adapters.zepp.sync_coordinator import ZeppSyncCoordinator

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
            repository=repository,
            options={
                "decode_dense_files": decode_dense_files,
                **({"detail_backfill": True} if detail_backfill else {}),
                **({"workout_only": True} if workout_only else {}),
                **({"detail_only": True} if detail_only else {}),
                **({"detail_refresh_before": detail_refresh_before} if detail_refresh_before is not None else {}),
                **({"detail_limit": detail_limit} if detail_limit is not None else {}),
                "source_mode": self.source_mode,
            },
        )

    def sync_with_report(
        self, user: User, days: int = 730, repo=None,
        decode_dense_files: bool = False, max_chunks: int | None = None,
        window: FetchWindow | None = None, trigger: str = "manual",
        trigger_ref: str | None = None, attempt_id: str | None = None,
        detail_backfill: bool = False, workout_only: bool = False,
    ) -> SyncReport:
        """Coordinator facade; ``repo`` remains only for legacy mock callers."""
        if (detail_backfill or workout_only) and trigger != "manual":
            raise ValueError("historical workout options require a manual sync")
        if self.mock:
            if window is not None:
                if not isinstance(window, FetchWindow):
                    raise ZeppAuthError("同步窗口格式无效", kind="invalid_request")
                start = date.fromisoformat(window.start_day(settings.timezone))
                end = date.fromisoformat(window.end_day(settings.timezone))
                requested_days = (end - start).days + 1
            else:
                try:
                    requested_days = int(days)
                except (TypeError, ValueError) as exc:
                    raise ZeppAuthError(
                        f"同步天数必须在 1..{MAX_SYNC_DAYS} 之间",
                        kind="invalid_request",
                    ) from exc
                if not 1 <= requested_days <= MAX_SYNC_DAYS:
                    raise ZeppAuthError(
                        f"同步天数必须在 1..{MAX_SYNC_DAYS} 之间",
                        kind="invalid_request",
                    )
                end = local_today()
                start = end - timedelta(days=requested_days - 1)
            if not 1 <= requested_days <= MAX_SYNC_DAYS:
                raise ZeppAuthError(
                    f"同步窗口必须覆盖 1..{MAX_SYNC_DAYS} 天",
                    kind="invalid_request",
                )
            if attempt_id is None:
                attempt = self.create_attempt(
                    user.id, days=days, window=window, trigger=trigger,
                    trigger_ref=trigger_ref, decode_dense_files=decode_dense_files,
                    detail_backfill=detail_backfill,
                    workout_only=workout_only,
                )
                attempt_id = attempt.id
            from vitalis.adapters.persistence import HealthRepository, session_scope
            with session_scope() as db:
                stored = HealthRepository(db).sync_attempt(attempt_id, user_id=user.id)
                if stored is None or (stored.options or {}).get("source_mode") != self.source_mode:
                    raise ZeppAuthError("排队任务来源模式与当前连接器不一致", kind="invalid_request")
            if repo:
                repo.bind_source_mode(user.id, self.source_mode)
            dailies = self._mock_fetch(user, start, end)
            if repo:
                for d in dailies:
                    repo.save_daily(d)
            progress = {"attempt_id": attempt_id, "status": "succeeded", "retry": 0}
            if attempt_id:
                from uuid import uuid4
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
        from vitalis.adapters.zepp.sync_coordinator import ZeppSyncCoordinator

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
                options={
                    "decode_dense_files": decode_dense_files,
                    **({"detail_backfill": True} if detail_backfill else {}),
                    **({"workout_only": True} if workout_only else {}),
                },
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
        from vitalis.adapters.persistence import session_scope, HealthRepository
        with session_scope() as db:
            return self._rebuild_dailies(HealthRepository(db), user.id, start, end)

    def _mock_fetch(self, user: User, start: date, end: date) -> list[NormalizedDaily]:
        """mock 模式保持原有逻辑（确定性模拟数据）。"""
        client = self._mock_client
        band = client.fetch_band_data(start.isoformat(), end.isoformat(), "detail", 8, 0)
        sleeps, activities = self.parser.parse_band(band)
        from vitalis.time import local_day_utc_bounds

        start_at, _ = local_day_utc_bounds(start, settings.timezone)
        _, end_at = local_day_utc_bounds(end, settings.timezone)
        workouts = []
        for sport in SPORTS:
            payload = client.fetch_sport_history(
                sport, int(start_at.timestamp()), int(end_at.timestamp()), 1
            )
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
                    total_load=(
                        sum(w.load for w in day_workouts)
                        if all(w.load is not None for w in day_workouts)
                        else None
                    ),
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
        from vitalis.domain import ActivityRecord, SleepRecord, TrainingRecord
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
                "POST /api/connect/zepp/token 导入"
            )
        vendor_id = auth.source_user_id or ""
        if not vendor_id:
            raise AuthRequired(
                f"用户 {user.id} 的 Zepp 凭据缺少厂商用户 id，请重新配对"
            )
        return ZeppAPIClient(
            app_token=auth.access_token,
            user_id=vendor_id,
            region_host=auth.region_host or DEFAULT_REGION,
        )

    # ---- 兼容基类（非扫码场景用配置 token 直连） ----
    def authenticate(self) -> ConnectorAuth:
        if self.mock:
            self.auth = ConnectorAuth(token="mock-token", extra={"mode": "mock"})
        else:
            self.auth = ConnectorAuth(token=settings.zepp_access_token, extra={"mode": "apptoken"})
        return self.auth
