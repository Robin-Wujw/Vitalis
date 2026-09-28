from datetime import datetime, timezone

import pytest

from vitalis.application.bridge import BridgeAuthenticationError, BridgeBatchIngest
from vitalis.application.ports import BridgeDeviceCapability


class _Repository:
    def __init__(self, state, capability=None):
        self.state = state
        self.capability = capability
        self.written = None

    def create_device_link(self, token_digest, user_id, device_label="balance2_zepp_os"):
        self.created = (token_digest, user_id, device_label)

    def write_bridge_batch(self, token_digest, seen_at, sample_factory):
        assert self.state["active"] is True
        self.call = (token_digest, seen_at)
        if self.capability is None:
            return None
        self.written = sample_factory(self.capability)
        return self.capability


class _UnitOfWork:
    def __init__(self, repository):
        self.repository = repository
        self.committed = False
        self.exit_error = None

    def __enter__(self):
        self.repository.state["active"] = True
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.exit_error = exc_value
        self.repository.state["active"] = False
        return False

    def commit(self):
        self.committed = True


def _sample_id(timestamp, ordinal=0, nonce="unit"):
    return f"z2:{timestamp}:{ordinal}:{nonce}:1"


def test_bridge_application_builds_normalized_samples_and_settles_partial_batch():
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    state = {"active": False}
    repository = _Repository(
        state,
        BridgeDeviceCapability(user_id="owner", device_label="balance2_zepp_os"),
    )
    unit_of_work = _UnitOfWork(repository)
    service = BridgeBatchIngest(lambda: unit_of_work, now_factory=lambda: now)

    result = service.ingest(
        "digest",
        2,
        [
            {
                "sample_id": _sample_id(now_ms - 1000),
                "timestamp": now_ms - 1000,
                "sample_ordinal": 0,
                "heart_rate": 72,
            },
            {
                "sample_id": _sample_id(now_ms - 1000, 1),
                "timestamp": now_ms - 1000,
                "sample_ordinal": 1,
                "heart_rate": 74,
            },
            {
                "sample_id": _sample_id(now_ms - 1000),
                "timestamp": now_ms - 1000,
                "sample_ordinal": 0,
                "heart_rate": 72,
            },
            "not-an-object",
        ],
    )

    assert result.as_dict()["acknowledged_count"] == 1
    assert result.as_dict()["rejected_count"] == 2
    assert {item["code"] for item in result.as_dict()["rejected"]} == {
        "duplicate_sample_id",
        "invalid_sample",
    }
    assert unit_of_work.committed is True
    assert unit_of_work.exit_error is None
    assert state["active"] is False
    assert [(sample.value, sample.sample_ordinal) for sample in repository.written] == [
        (74, 1),
    ]
    assert {sample.user_id for sample in repository.written} == {"owner"}
    assert {sample.device_id for sample in repository.written} == {"balance2_zepp_os"}
    assert {sample.timestamp for sample in repository.written} == {
        datetime.fromtimestamp((now_ms - 1000) / 1000, tz=timezone.utc)
    }


def test_bridge_application_does_not_commit_when_capability_is_invalid():
    state = {"active": False}
    repository = _Repository(state)
    unit_of_work = _UnitOfWork(repository)
    service = BridgeBatchIngest(
        lambda: unit_of_work,
        now_factory=lambda: datetime(2026, 9, 1, tzinfo=timezone.utc),
    )

    with pytest.raises(BridgeAuthenticationError):
        service.ingest("revoked", 2, [])

    assert unit_of_work.committed is False
    assert unit_of_work.exit_error is not None
    assert state["active"] is False
