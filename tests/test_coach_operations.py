from datetime import date, datetime, timedelta, timezone
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session
from vitalis.adapters.persistence.database import _create_engine, init_db
from vitalis.adapters.persistence.coach_requests import CoachRequest, CoachRequestRepository
from vitalis.adapters.persistence.repositories import HealthRepository
from vitalis.application.coach_operations import CoachError, CoachOperationService, CoachPolicy
from vitalis.application.sync_jobs import SyncJobAuthRequired

import pytest
from pydantic import ValidationError

from vitalis.application.coach_operations import CoachOperation, CoachPolicyFile, load_policy


NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
DAY = NOW.date()


@pytest.fixture
def coach(tmp_path):
    engine = _create_engine(f"sqlite:///{(tmp_path / 'coach.db').as_posix()}")
    init_db(engine)

    @contextmanager
    def scope():
        with Session(engine, expire_on_commit=False) as db, db.begin():
            yield db

    with scope() as db:
        for name in ('alice', 'bob'):
            HealthRepository(db).upsert_user(name)
    policy = CoachPolicy(enabled=True, revision='r1', expires_at=NOW + timedelta(days=1),
                         allow_refresh=True, allow_analyze=True)
    sync = SimpleNamespace(create=lambda command: SimpleNamespace(job_id='sync-' + command.idempotency_key))
    store = CoachRequestRepository(scope, sync)
    service = CoachOperationService(store, policy_loader=lambda user: policy,
                                    today=lambda: DAY, clock=lambda: NOW)
    yield service, store, scope, sync
    engine.dispose()


def op(kind='analyze', day=DAY):
    return CoachOperation(operation=kind, day=day)


@pytest.mark.parametrize('denial', ['revoked', 'expired', 'old_day', 'grant_removed'])
@pytest.mark.parametrize('kind', ['analyze', 'refresh_workouts'])
def test_accepted_replay_precedes_current_authorization(coach, denial, kind):
    service, store, scope, _ = coach
    request = op(kind)
    first = service.create('alice', 'accepted-replay-key', request)
    if denial == 'revoked':
        service.policy_loader = lambda user: CoachPolicy()
    elif denial == 'expired':
        service.clock = lambda: NOW + timedelta(days=2)
    elif denial == 'old_day':
        service.today = lambda: DAY + timedelta(days=29)
    else:
        service.policy_loader = lambda user: CoachPolicy(enabled=True, revision='r2',
            expires_at=NOW + timedelta(days=1))
    # Replay must not even enter dispatch, including its downstream adapters.
    store.dispatch = lambda reservation: pytest.fail('replay dispatched')
    replay = service.create('alice', 'accepted-replay-key', request)
    assert replay['job_id'] == first['job_id']
    with pytest.raises(CoachError) as error:
        service.create('alice', 'accepted-replay-key', op(kind, DAY - timedelta(days=1)))
    assert error.value.status_code == 409
    with scope() as db:
        assert len(list(db.scalars(select(CoachRequest)))) == 1


@pytest.mark.parametrize('kind', ['analyze', 'refresh_workouts'])
def test_unlinked_accepted_job_recovers_without_authorization_or_dispatch(coach, kind):
    import hashlib
    from vitalis.adapters.persistence.models import SyncAttempt
    service, store, scope, _ = coach
    first = service.create('alice', 'recover-link-key', op(kind))
    with scope() as db:
        row = db.get(CoachRequest, first['request_id'])
        row.job_id, row.error = None, 'dispatch_pending'
        if kind == 'refresh_workouts':
            db.add(SyncAttempt(id=first['job_id'], user_id='alice', source='zepp',
                trigger='manual', trigger_ref='api:' + hashlib.sha256(
                    ('coach:' + row.id).encode()).hexdigest(), request_key='synthetic',
                window_start=NOW.replace(tzinfo=None),
                window_end=(NOW + timedelta(days=1)).replace(tzinfo=None), status='succeeded'))
    service.policy_loader = lambda user: CoachPolicy()
    store.dispatch = lambda reservation: pytest.fail('recovery dispatched')
    result = service.create('alice', 'recover-link-key', op(kind))
    assert result['job_id'] == first['job_id']
    assert result['error'] is None
    with scope() as db:
        assert db.get(CoachRequest, first['request_id']).job_id == first['job_id']


def test_denied_uncertain_root_keeps_reservation_without_enqueue(coach):
    service, store, scope, sync = coach
    sync.create = lambda command: (_ for _ in ()).throw(TimeoutError())
    with pytest.raises(CoachError):
        service.create('alice', 'denied-uncertain-key', op('refresh_workouts'))
    granted = service.policy_loader('alice')
    service.policy_loader = lambda user: CoachPolicy()
    store.dispatch = lambda reservation: pytest.fail('denied root dispatched')
    with pytest.raises(CoachError) as error:
        service.create('alice', 'denied-uncertain-key', op('refresh_workouts'))
    assert error.value.status_code == 403
    assert error.value.failure_code == 'dispatch_pending'
    with scope() as db:
        row = db.scalar(select(CoachRequest))
        assert row.state == 'queued' and row.reserved_units == 1 and row.job_id is None
        row.state = 'failed'  # Proven terminal outcome still counts toward hourly budget.
    service.policy_loader = lambda user: granted.model_copy(update={'max_requests_per_hour': 1})
    with pytest.raises(CoachError) as error:
        service.create('alice', 'new-after-denied-key', op())
    assert error.value.status_code == 429


@pytest.mark.parametrize('kind', ['analyze', 'refresh_workouts'])
@pytest.mark.parametrize('denial', ['revoked', 'old_day'])
def test_unlinked_reservation_without_job_never_dispatches_when_denied(coach, kind, denial):
    import hashlib
    service, store, scope, _ = coach
    request = op(kind)
    store.reserve('alice', 'reserved-only-key', request,
        hashlib.sha256(request.model_dump_json().encode()).hexdigest(),
        service.policy_loader('alice'), NOW)
    if denial == 'revoked':
        service.policy_loader = lambda user: CoachPolicy()
    else:
        service.today = lambda: DAY + timedelta(days=29)
    store.dispatch = lambda reservation: pytest.fail('unlinked denied root dispatched')
    with pytest.raises(CoachError) as error:
        service.create('alice', 'reserved-only-key', request)
    assert error.value.status_code == (403 if denial == 'revoked' else 422)
    assert error.value.failure_code == 'dispatch_pending'
    assert error.value.next_action == 'check_resource'
    assert not error.value.retryable
    with scope() as db:
        row = db.scalar(select(CoachRequest))
        assert row.job_id is None and row.state == 'queued'
        assert row.reserved_units == 1


def test_new_key_recovers_terminal_unlinked_root_but_preserves_hourly_count(coach):
    from vitalis.adapters.persistence.models import AnalysisJob
    service, _, scope, _ = coach
    first = service.create('alice', 'terminal-unlinked-key', op())
    with scope() as db:
        db.get(CoachRequest, first['request_id']).job_id = None
        db.get(AnalysisJob, first['job_id']).status = 'succeeded'
    second = service.create('alice', 'after-unlinked-key', op(day=DAY - timedelta(days=1)))
    with scope() as db:
        row = db.get(CoachRequest, first['request_id'])
        assert row.job_id == first['job_id'] and row.state == 'succeeded'
        db.get(AnalysisJob, second['job_id']).status = 'succeeded'
    granted = service.policy_loader('alice')
    service.policy_loader = lambda user: granted.model_copy(update={'max_requests_per_hour': 2})
    with pytest.raises(CoachError) as error:
        service.create('alice', 'over-hourly-limit-key', op())
    assert error.value.status_code == 429


def test_failed_root_replays_after_revocation(coach):
    from vitalis.adapters.persistence.models import AnalysisJob
    service, store, scope, _ = coach
    first = service.create('alice', 'failed-revoked-key', op())
    with scope() as db:
        db.get(AnalysisJob, first['job_id']).status = 'failed'
    service.policy_loader = lambda user: CoachPolicy()
    store.dispatch = lambda reservation: pytest.fail('failed replay dispatched')
    with pytest.raises(CoachError) as error:
        service.create('alice', 'failed-revoked-key', op())
    assert error.value.status_code == 409
    assert error.value.failure_code == 'operation_failed'


def test_policy_missing_is_default_deny(tmp_path):
    assert load_policy("alice", path=tmp_path / "absent.json").enabled is False


def test_policy_forbids_unknown_and_naive_expiry():
    with pytest.raises(ValidationError):
        CoachPolicyFile.model_validate({"users": {}, "silent": True})
    with pytest.raises(ValidationError):
        CoachPolicyFile.model_validate({"users": {"alice": {"enabled": True, "revision": "r1", "expires_at": "2099-01-01T00:00:00"}}})


@pytest.mark.parametrize("day", ["2026-10-30T00:00:00", 1793318400])
def test_operation_requires_exact_day(day):
    with pytest.raises(ValidationError):
        CoachOperation.model_validate({"operation": "analyze", "day": day})


def test_client_cannot_override_policy():
    with pytest.raises(ValidationError):
        CoachOperation.model_validate({"operation": "analyze", "day": "2026-10-30", "notification_policy": "suppress"})


def test_replay_conflict_and_cross_user(coach):
    service, _, scope, _ = coach
    first = service.create('alice', 'stable-request-key', op())
    assert service.create('alice', 'stable-request-key', op())['job_id'] == first['job_id']
    other = service.create('bob', 'stable-request-key', op())
    assert other['request_id'] != first['request_id']
    assert other['job_id'] != first['job_id']
    with pytest.raises(CoachError) as error:
        service.create('alice', 'stable-request-key', op(day=DAY - timedelta(days=1)))
    assert error.value.status_code == 409
    with scope() as db:
        assert len(list(db.scalars(select(CoachRequest)))) == 2


def test_pending_root_blocks_new_keys_even_after_hour(coach):
    service, _, _, _ = coach
    service.create('alice', 'pending-request-1', op('refresh_workouts'))
    service.clock = lambda: NOW + timedelta(hours=2)
    with pytest.raises(CoachError) as error:
        service.create('alice', 'pending-request-2', op())
    assert error.value.status_code == 429


def test_hourly_budget_counts_terminal_roots(coach):
    service, _, scope, _ = coach
    from vitalis.adapters.persistence.models import AnalysisJob
    for i in range(3):
        result = service.create('alice', f'hourly-request-{i:02}', op(day=DAY - timedelta(days=i)))
        with scope() as db:
            db.get(AnalysisJob, result['job_id']).status = 'succeeded'
    with pytest.raises(CoachError) as error:
        service.create('alice', 'hourly-request-04', op(day=DAY - timedelta(days=4)))
    assert error.value.status_code == 429
    service.clock = lambda: NOW + timedelta(hours=1, seconds=1)
    assert service.create('alice', 'hourly-request-04', op(day=DAY - timedelta(days=4)))['job_id']


def test_concurrent_new_roots_are_serialized(coach):
    service, _, scope, _ = coach
    def create(i):
        try:
            return service.create('alice', f'parallel-key-{i:04}', op())
        except CoachError as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(create, range(4)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count(429) == 3
    with scope() as db:
        assert len(list(db.scalars(select(CoachRequest)))) == 1


def test_known_enqueue_failure_is_terminal_not_success(coach):
    service, _, scope, sync = coach
    calls = []
    def reject(command):
        calls.append(command)
        raise SyncJobAuthRequired('private credential detail')
    sync.create = reject
    for _ in range(2):
        with pytest.raises(CoachError) as error:
            service.create('alice', 'rejected-request', op('refresh_workouts'))
        assert error.value.status_code == 409
        assert 'private' not in str(error.value)
    assert len(calls) == 1
    with scope() as db:
        row = db.scalar(select(CoachRequest))
        assert row.state == 'failed' and row.error == 'source_auth_required'


def test_uncertain_enqueue_preserves_budget_and_same_key(coach):
    service, _, scope, sync = coach
    calls = []
    def uncertain(command):
        calls.append(command)
        if len(calls) == 1:
            raise TimeoutError('private upstream text')
        return SimpleNamespace(job_id='accepted-before-timeout')
    sync.create = uncertain
    with pytest.raises(CoachError) as error:
        service.create('alice', 'uncertain-request', op('refresh_workouts'))
    assert error.value.status_code == 503
    assert error.value.retryable
    with pytest.raises(CoachError) as error:
        service.create('alice', 'different-request', op())
    assert error.value.status_code == 429
    accepted = service.create('alice', 'uncertain-request', op('refresh_workouts'))
    assert accepted['job_id'] == 'accepted-before-timeout'
    assert calls[0].idempotency_key == calls[1].idempotency_key
    assert calls[0].detail_limit is None and calls[0].workout_only
    assert calls[0].execution_context['notification_policy'] == 'suppress'
    with scope() as db:
        assert len(list(db.scalars(select(CoachRequest)))) == 1


@pytest.mark.parametrize('policy', [CoachPolicy(), CoachPolicy(enabled=True, revision='old', expires_at=NOW)])
def test_disabled_or_expired_policy_rejects_before_reservation(coach, policy):
    service, _, scope, _ = coach
    service.policy_loader = lambda user: policy
    assert service.policy('alice')['enabled'] is False
    with pytest.raises(CoachError) as error:
        service.create('alice', 'denied-request-1', op())
    assert error.value.status_code == 403
    with scope() as db:
        assert list(db.scalars(select(CoachRequest))) == []


@pytest.mark.parametrize('kind,age', [('analyze', 28), ('analyze', -1), ('refresh_workouts', 2)])
def test_date_bounds(coach, kind, age):
    service, _, _, _ = coach
    with pytest.raises(CoachError) as error:
        service.create('alice', 'out-of-range-key', op(kind, DAY - timedelta(days=age)))
    assert error.value.status_code == 422


def test_policy_file_is_user_scoped_and_reloaded(tmp_path):
    path = tmp_path / 'policy.json'
    path.write_text(CoachPolicyFile(users={'alice': CoachPolicy(enabled=True, revision='v1',
        expires_at=NOW + timedelta(days=1), allow_analyze=True)}).model_dump_json(), encoding='utf-8')
    import os
    if os.name == 'posix' and os.geteuid() != 0:
        assert not load_policy('alice', path=path).enabled
        return  # Non-root test-owned files correctly cannot grant server policy.
    assert load_policy('alice', path=path).revision == 'v1'
    assert not load_policy('bob', path=path).enabled
    path.write_text('{invalid', encoding='utf-8')
    assert not load_policy('alice', path=path).enabled


def test_enabled_policy_requires_revision_and_expiry():
    for fields in ({'enabled': True}, {'enabled': True, 'expires_at': NOW}):
        with pytest.raises(ValidationError):
            CoachPolicy.model_validate(fields)


def test_relative_policy_path_is_denied():
    assert not load_policy('alice', path='policy.json').enabled


def test_analysis_context_is_persisted(coach):
    service, _, scope, _ = coach
    result = service.create('alice', 'analysis-context-key', op())
    from vitalis.adapters.persistence.models import AnalysisJob
    with scope() as db:
        job = db.get(AnalysisJob, result['job_id'])
        assert job.execution_context == {
            'origin': 'conversation', 'root_id': result['request_id'],
            'notification_policy': 'suppress', 'policy_revision': 'r1',
        }


def test_concurrent_same_key_has_one_durable_root(coach):
    service, _, scope, _ = coach
    def create(_):
        return service.create('alice', 'same-concurrent-key', op())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(create, range(4)))
    assert len({result['request_id'] for result in results}) == 1
    assert len({result['job_id'] for result in results}) == 1
    with scope() as db:
        assert len(list(db.scalars(select(CoachRequest)))) == 1


def test_operation_grants_are_independent(coach):
    service, _, _, _ = coach
    service.policy_loader = lambda user: CoachPolicy(enabled=True, revision='limited',
        expires_at=NOW + timedelta(days=1), allow_refresh=False, allow_analyze=False)
    for kind in ('refresh_workouts', 'analyze'):
        with pytest.raises(CoachError) as error:
            service.create('alice', 'limited-permission', op(kind))
        assert error.value.status_code == 403


def test_failed_root_replay_uses_error_not_queued_response(coach):
    service, _, scope, _ = coach
    result = service.create('alice', 'failed-root-key-1', op())
    from vitalis.adapters.persistence.models import AnalysisJob
    with scope() as db:
        db.get(AnalysisJob, result['job_id']).status = 'failed'
    with pytest.raises(CoachError) as error:
        service.create('alice', 'failed-root-key-1', op())
    assert error.value.status_code == 409
    assert error.value.failure_code == 'operation_failed'


@pytest.mark.parametrize('field,value', [('max_lookback_days', 29), ('max_inflight', 2),
                                       ('max_requests_per_hour', 4)])
def test_policy_cannot_exceed_server_ceiling(field, value):
    with pytest.raises(ValidationError):
        CoachPolicy.model_validate({field: value})
