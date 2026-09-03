"""Persistent worker for durable Vitalis sync attempts."""

from __future__ import annotations

import logging
import signal
from threading import Event

from vitalis.config import settings
from vitalis.scheduler.jobs import dispatcher_job
from vitalis.storage import init_db

log = logging.getLogger("vitalis.worker")


def run(stop_event: Event | None = None) -> None:
    """Drain due sync chunks until the process receives a stop signal."""
    stopped = stop_event or Event()
    init_db()
    interval = max(1, settings.sync_dispatcher_interval_seconds)
    while not stopped.is_set():
        try:
            dispatcher_job()
        except Exception:
            log.exception("sync worker pass failed")
        stopped.wait(interval)


def main() -> None:
    stopped = Event()

    def stop(_signum, _frame) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    run(stopped)


if __name__ == "__main__":
    main()
