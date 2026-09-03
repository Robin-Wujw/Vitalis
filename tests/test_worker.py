from threading import Event

from vitalis import worker


def test_worker_initializes_storage_and_repeats_dispatch(monkeypatch):
    stopped = Event()
    calls = []

    monkeypatch.setattr(worker, "init_db", lambda: calls.append("init"))
    monkeypatch.setattr(worker.settings, "sync_dispatcher_interval_seconds", 1)

    def dispatch():
        calls.append("dispatch")
        stopped.set()

    monkeypatch.setattr(worker, "dispatcher_job", dispatch)

    worker.run(stopped)

    assert calls == ["init", "dispatch"]


def test_worker_continues_after_dispatch_failure(monkeypatch):
    stopped = Event()
    calls = []

    monkeypatch.setattr(worker, "init_db", lambda: None)
    monkeypatch.setattr(worker.settings, "sync_dispatcher_interval_seconds", 1)

    def dispatch():
        calls.append("dispatch")
        if len(calls) == 1:
            raise RuntimeError("transient failure")
        stopped.set()

    monkeypatch.setattr(worker, "dispatcher_job", dispatch)

    worker.run(stopped)

    assert calls == ["dispatch", "dispatch"]
