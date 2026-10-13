"""Per-run peak memory measurement for the daily analysis budget.

Linux exposes the process high-water mark (``VmHWM``) and lets the process
reset it through ``/proc/self/clear_refs``.  Elsewhere the peak is unknown.
"""

from __future__ import annotations

from pathlib import Path

_STATUS = Path("/proc/self/status")
_CLEAR_REFS = Path("/proc/self/clear_refs")


def reset_peak_rss() -> bool:
    """Start a new peak window; return False when the platform cannot."""
    try:
        _CLEAR_REFS.write_text("5")
    except OSError:
        return False
    return True


def _status_mb(field: str) -> float | None:
    try:
        for line in _STATUS.read_text().splitlines():
            if line.startswith(field):
                return round(int(line.split()[1]) / 1024, 1)
    except (OSError, ValueError, IndexError):
        return None
    return None


def peak_rss_mb() -> float | None:
    return _status_mb("VmHWM:")


def current_rss_mb() -> float | None:
    return _status_mb("VmRSS:")
