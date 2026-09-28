"""Pure classification boundary for Zepp workout-detail payloads.

ZeppParser remains the single implementation of workout-detail normalization.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import json
from typing import Literal

from vitalis.domain import WorkoutDetail

from ..parser import ZeppParser


ParseStatus = Literal["recognized", "empty", "unrecognized"]


@dataclass(frozen=True, slots=True)
class ParseContext:
    summary_end: datetime | None = None
    training_family: str | None = None


@dataclass(frozen=True, slots=True)
class ParseResult:
    status: ParseStatus
    detail: WorkoutDetail | None = None
    # Static codes only: vendor values (including free-text memo) never enter logs.
    diagnostics: tuple[str, ...] = ()


# Only fields accepted by the existing normalizer and observed vendor metadata.
_DETAIL_FIELDS = frozenset({
    "trackid", "time", "heart_rate", "speed", "equivPace",
    "currentDistance", "time_delta_altitude", "power_meter", "gait",
    "runPosture", "lap", "pause", "strengthSets", "strengthAssess",
    "memo", "source",
})
_ENVELOPE_FIELDS = frozenset({"data", "code", "message"})


def parse_workout_detail(
    payload: Mapping[str, object], context: ParseContext
) -> ParseResult:
    """Recognize known Zepp detail, explicit empty detail, or an unsafe shape.

    Resource limit errors from ZeppParser propagate to the sync layer; callers
    report them separately from transport failures and from unknown payloads.
    """
    if not isinstance(payload, Mapping):
        return ParseResult("unrecognized", diagnostics=("invalid_payload",))

    root = dict(payload)
    if "data" in root:
        if not isinstance(root["data"], Mapping):
            return ParseResult("unrecognized", diagnostics=("invalid_envelope",))
        data = dict(root["data"])
        root["data"] = data
        if root.get("code", 0) not in (0, 1, "0", "1"):
            return ParseResult("unrecognized", diagnostics=("vendor_error_envelope",))
        unknown_envelope = any(
            key not in _ENVELOPE_FIELDS and not _empty(value)
            for key, value in root.items()
        )
    else:
        data = root
        unknown_envelope = False

    # The existing parser accepts dicts (not arbitrary Mapping instances). A
    # shallow copy allows Mapping callers without changing its parse behavior.
    try:
        detail = ZeppParser.parse_workout_detail(
            root, summary_end=context.summary_end,
            training_family=context.training_family,
        )
    except Exception as exc:
        from ..parser import WorkoutDetailLimitError

        if isinstance(exc, WorkoutDetailLimitError):
            raise
        return ParseResult("unrecognized", diagnostics=("malformed_detail",))

    if detail is None:
        return ParseResult("unrecognized", diagnostics=("invalid_trackid",))

    if detail.samples or detail.laps or detail.pauses or detail.strength_sets:
        return ParseResult("recognized", detail=detail)

    if unknown_envelope:
        return ParseResult("unrecognized", diagnostics=("unknown_envelope",))
    if any(key not in _DETAIL_FIELDS and not _empty(value) for key, value in data.items()):
        return ParseResult("unrecognized", diagnostics=("unknown_detail_field",))
    if all(_known_empty(key, value) for key, value in data.items()):
        return ParseResult("empty", detail=detail)
    return ParseResult("unrecognized", diagnostics=("unparsed_nonempty_detail",))


def _known_empty(key: str, value: object) -> bool:
    if key == "trackid":
        return True
    if key == "source":
        return value is None or isinstance(value, str)
    if key in {"strengthSets", "strengthAssess"}:
        if isinstance(value, str):
            if not value.strip():
                return True
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                return False
        # Assessment is known but not normalized; do not interpret its codes.
        if key == "strengthAssess":
            return isinstance(value, (list, dict))
        return value == []
    if key == "memo":
        return value is None or (isinstance(value, str) and not value.strip())
    if key == "lap" and isinstance(value, str) and value.strip():
        # The legacy parser recognizes this exact vendor layout only for
        # strength workouts. Other families retain an explicit empty detail.
        rows = [row for row in value.split(";") if row]
        return bool(rows) and all(_known_lap_62_row(row) for row in rows)
    return _empty(value)


def _known_lap_62_row(row: str) -> bool:
    fields = row.split(",")
    if len(fields) != 62:
        return False
    try:
        return int(fields[0]) >= 0 and float(fields[1]) >= 0 and float(fields[2]) < 0
    except ValueError:
        return False


def _empty(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return value == [] or value == {}
