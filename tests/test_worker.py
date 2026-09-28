from threading import Event, Timer

from vitalis.entrypoints import worker
from vitalis.scheduler import jobs


def test_worker_owns_scheduler_lifecycle(monkeypatch):
    stopped = Event()
    stopped.set()
    calls = []

    class FakeScheduler:
        def shutdown(self, *, wait):
            calls.append(("shutdown", wait))

    monkeypatch.setattr(worker, "check_schema", lambda: calls.append("init"))
    monkeypatch.setattr(
        worker, "start_scheduler", lambda: calls.append("start") or FakeScheduler()
    )

    worker.run(stopped)

    assert calls == ["init", "start", ("shutdown", False)]


def test_worker_retries_failed_dispatch_on_interval(monkeypatch):
    stopped = Event()
    calls = []

    def dispatch():
        calls.append("dispatch")
        if len(calls) == 1:
            raise RuntimeError("transient failure")
        stopped.set()

    monkeypatch.setattr(worker, "check_schema", lambda: None)
    monkeypatch.setattr(worker, "start_scheduler", jobs.start_scheduler)
    monkeypatch.setattr(jobs, "dispatcher_job", dispatch)
    monkeypatch.setattr(jobs.settings, "sync_dispatcher_interval_seconds", 1)

    timeout = Timer(5, stopped.set)
    timeout.start()
    try:
        worker.run(stopped)
    finally:
        timeout.cancel()

    assert calls == ["dispatch", "dispatch"]
