"""Persistent worker for scheduled Vitalis sync attempts."""

from __future__ import annotations

import signal
from threading import Event

from vitalis.scheduler import start_scheduler
from vitalis.adapters.persistence.database import check_schema


def run(stop_event: Event | None = None) -> None:
    """Run scheduled jobs until the process receives a stop signal."""
    stopped = stop_event if stop_event is not None else Event()
    check_schema()
    scheduler = start_scheduler()
    try:
        stopped.wait()
    finally:
        scheduler.shutdown(wait=False)


def main() -> None:
    stopped = Event()

    def stop(_signum, _frame) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    run(stopped)


if __name__ == "__main__":
    main()
