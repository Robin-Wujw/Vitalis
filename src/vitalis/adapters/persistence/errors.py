"""Recognize retryable storage contention without inspecting private error text."""

import sqlite3

from sqlalchemy.exc import OperationalError


def is_sqlite_contention(error: BaseException) -> bool:
    original = error.orig if isinstance(error, OperationalError) else error
    if not isinstance(original, sqlite3.Error):
        return False
    code = getattr(original, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
