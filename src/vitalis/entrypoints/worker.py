"""Persistent worker for scheduled Vitalis sync attempts."""

from __future__ import annotations

import faulthandler
import logging
import signal
from threading import Event

from vitalis.scheduler import start_scheduler
from vitalis.adapters.persistence.database import check_schema


def configure_logging() -> None:
    """Send Vitalis INFO lines to stderr (the service journal); APScheduler stays at WARNING."""
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.WARNING, handlers=[handler])
    logging.getLogger("vitalis").setLevel(logging.INFO)
    # A watchdog SIGABRT then prints every thread's stack before the process exits.
    faulthandler.enable()


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
    configure_logging()
    stopped = Event()

    def stop(_signum, _frame) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    run(stopped)


if __name__ == "__main__":
    main()
