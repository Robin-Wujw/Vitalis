"""Pure classification boundary for Zepp workout-detail payloads.

ZeppParser remains the single implementation of workout-detail normalization.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from typing import Iterator, Literal

from vitalis.domain import (
    WorkoutDetail,
    WorkoutLap,
    WorkoutMetricSample,
    WorkoutPause,
)

from ..parser import (
    MAX_WORKOUT_DETAIL_SAMPLES,
    MAX_WORKOUT_SECONDS,
    WorkoutDetailLimitError,
    ZeppParser,
)
from ..workout_streaming import (
    DelimitedText,
    SpoolPayload,
    WorkoutStreamError,
    WorkoutStreamLimitError,
    scan_workout_payload,
)


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
    if isinstance(payload, SpoolPayload):
        return parse_workout_detail_stream(payload, context)
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
    is_empty = getattr(value, "is_empty", None)
    if callable(is_empty):
        return bool(is_empty())
    return value == [] or value == {}


def parse_workout_detail_stream(
    payload: SpoolPayload,
    context: ParseContext,
) -> ParseResult:
    """Parse a spooled detail body without materializing its large strings."""
    try:
        fields = scan_workout_payload(
            payload,
            max_objects=20_000,
            max_separators=80_000,
        )
        root = {key: ref.as_value(key) for key, ref in fields.items()}
        nested = root.get("data")
        data = nested if isinstance(nested, dict) else root
        code = root.get("code")
        if code not in (None, 0, 1, "0", "1"):
            return ParseResult("unrecognized", diagnostics=("vendor_error_envelope",))
        if nested is not None and not isinstance(nested, dict):
            return ParseResult("unrecognized", diagnostics=("invalid_envelope",))
        unknown_envelope = any(
            key not in _ENVELOPE_FIELDS and not _empty(value)
            for key, value in root.items()
        )
        detail = _normalize_stream_detail(data, context)
    except WorkoutStreamLimitError as exc:
        raise WorkoutDetailLimitError(str(exc)) from exc
    except WorkoutDetailLimitError:
        # A size limit is a resource limit, not an unrecognized payload.
        raise
    except (WorkoutStreamError, ValueError, TypeError, OverflowError):
        return ParseResult("unrecognized", diagnostics=("malformed_detail",))

    if detail is None:
        return ParseResult("unrecognized", diagnostics=("invalid_trackid",))
    if detail.samples or detail.laps or detail.pauses or detail.strength_sets:
        return ParseResult("recognized", detail=detail)
    if unknown_envelope:
        return ParseResult("unrecognized", diagnostics=("unknown_envelope",))
    if any(key not in _DETAIL_FIELDS and not _empty(value) for key, value in data.items()):
        return ParseResult("unrecognized", diagnostics=("unknown_detail_field",))
    if all(_stream_known_empty(key, value) for key, value in data.items()):
        return ParseResult("empty", detail=detail)
    return ParseResult("unrecognized", diagnostics=("unparsed_nonempty_detail",))


def _stream_known_empty(key: str, value: object) -> bool:
    if key in {"trackid", "source"}:
        return True
    if key in {"strengthSets", "strengthAssess"}:
        if isinstance(value, DelimitedText):
            if value.is_empty():
                return True
            try:
                parsed = value.json_value()
            except WorkoutStreamError:
                return False
            return isinstance(parsed, (list, dict))
        if key == "strengthAssess":
            return isinstance(value, (list, dict))
        return value == []
    if key == "memo":
        return _empty(value)
    if key == "lap" and isinstance(value, DelimitedText):
        rows = [row for row in value.iter_parts() if row]
        return bool(rows) and all(_known_lap_62_row(row) for row in rows)
    return _empty(value)


def _stream_parts(value: object) -> Iterator[str]:
    if isinstance(value, DelimitedText):
        return value.iter_parts()
    if isinstance(value, str):
        return iter(value.split(";"))
    return iter(())


def _normalize_stream_detail(data: dict[str, object], context: ParseContext) -> WorkoutDetail | None:
    start = ZeppParser._parse_datetime_value(data.get("trackid"))
    if start is None:
        return None
    start = ZeppParser._utc(start)
    workout_id = str(data.get("trackid") or "")
    time_span = 0
    for part in _stream_parts(data.get("time")):
        try:
            time_span += max(int(part), 0)
        except (TypeError, ValueError):
            continue
    end = start + timedelta(seconds=min(time_span, MAX_WORKOUT_SECONDS))
    if context.summary_end is not None:
        end = max(end, ZeppParser._utc(context.summary_end))
    end = min(max(end, start + timedelta(seconds=1)), start + timedelta(seconds=MAX_WORKOUT_SECONDS))

    samples: list[WorkoutMetricSample] = []
    samples.extend(_stream_heart_rate(workout_id, start, end, data.get("heart_rate")))
    for field, metric, unit, scale, minimum, maximum in (
        ("speed", "speed", "m/s", 1.0, 0.0, 20.0),
        ("equivPace", "equivalent_pace", "s/km", 1.0, 60.0, 3600.0),
        ("currentDistance", "distance", "m", 0.01, 0.0, 1_000_000.0),
        ("time_delta_altitude", "altitude", "m", 0.1, -1000.0, 10_000.0),
        ("power_meter", "running_power", "W", 1.0, 0.0, 5000.0),
    ):
        samples.extend(_stream_absolute(
            workout_id, start, data.get(field), metric, unit, scale, minimum, maximum
        ))
    samples.extend(_stream_gait(workout_id, start, data.get("gait")))
    samples.extend(_stream_posture(workout_id, start, data.get("runPosture")))
    if len(samples) > MAX_WORKOUT_DETAIL_SAMPLES:
        raise WorkoutDetailLimitError("运动明细样本超过安全上限")
    samples = ZeppParser._deduplicate_workout_samples(samples)

    encoded_sets = data.get("strengthSets")
    if isinstance(encoded_sets, DelimitedText):
        try:
            encoded_sets = encoded_sets.json_value()
        except WorkoutStreamError:
            encoded_sets = []
    strength_sets = ZeppParser._strength_sets(encoded_sets)
    if not strength_sets and context.training_family == "strength":
        strength_sets = ZeppParser._strength_lap_rows(_stream_parts(data.get("lap")))
    counts: dict[str, int] = {}
    for sample in samples:
        counts[sample.metric] = counts.get(sample.metric, 0) + 1
    return WorkoutDetail(
        workout_id=workout_id,
        metrics_present=sorted(counts),
        metric_sample_counts=dict(sorted(counts.items())),
        laps=_stream_laps(data.get("lap")),
        pauses=_stream_pauses(data.get("pause")),
        strength_sets=strength_sets,
        samples=samples,
    )


def _stream_heart_rate(workout_id, start, end, encoded) -> list[WorkoutMetricSample]:
    if not isinstance(encoded, (str, DelimitedText)):
        return []
    samples: list[WorkoutMetricSample] = []
    working = start
    heart_rate = 0
    parsed = 0
    for part in _stream_parts(encoded):
        if not part:
            continue
        raw_seconds, separator, raw_delta = part.partition(",")
        if not separator:
            continue
        try:
            seconds = max(1 if raw_seconds == "" else int(raw_seconds), 0)
            delta = int(raw_delta)
        except (TypeError, ValueError):
            continue
        parsed += 1
        heart_rate += delta
        first_second = 0 if parsed == 1 else 1
        for _ in range(first_second, seconds + 1):
            if working > end:
                break
            if 1 <= heart_rate <= 300:
                samples.append(WorkoutMetricSample(
                    workout_id=workout_id, timestamp=working, metric="heart_rate",
                    value=float(heart_rate), unit="bpm",
                ))
            working += timedelta(seconds=1)
    if not parsed:
        return []
    while working <= end:
        if 1 <= heart_rate <= 300:
            samples.append(WorkoutMetricSample(
                workout_id=workout_id, timestamp=working, metric="heart_rate",
                value=float(heart_rate), unit="bpm",
            ))
        working += timedelta(seconds=1)
    return samples


def _stream_absolute(workout_id, start, encoded, metric, unit, scale, minimum, maximum):
    output = []
    elapsed = 0
    for part in _stream_parts(encoded):
        if not part:
            continue
        raw_seconds, separator, raw_value = part.partition(",")
        if not separator:
            continue
        try:
            elapsed += max(int(raw_seconds or 1), 0)
            value = float(raw_value) * scale
        except (TypeError, ValueError):
            continue
        if elapsed <= MAX_WORKOUT_SECONDS and minimum <= value <= maximum:
            output.append(WorkoutMetricSample(
                workout_id=workout_id, timestamp=start + timedelta(seconds=elapsed),
                metric=metric, value=round(value, 4), unit=unit,
            ))
    return output


def _stream_gait(workout_id, start, encoded):
    output, elapsed = [], 0
    for part in _stream_parts(encoded):
        fields = part.split(",")
        if len(fields) < 4:
            continue
        try:
            elapsed += max(int(fields[0] or 1), 0)
            stride, cadence = float(fields[-2]), float(fields[-1])
        except (TypeError, ValueError):
            continue
        if elapsed <= MAX_WORKOUT_SECONDS and 30 <= cadence <= 300:
            output.append(WorkoutMetricSample(
                workout_id=workout_id, timestamp=start + timedelta(seconds=elapsed),
                metric="cadence", value=cadence, unit="spm",
            ))
        if elapsed <= MAX_WORKOUT_SECONDS and 10 <= stride <= 300:
            output.append(WorkoutMetricSample(
                workout_id=workout_id, timestamp=start + timedelta(seconds=elapsed),
                metric="stride_length", value=stride, unit="cm",
            ))
    return output


def _stream_posture(workout_id, start, encoded):
    output, elapsed = [], 0
    for part in _stream_parts(encoded):
        fields = part.split(",")
        if len(fields) < 4:
            continue
        try:
            elapsed += max(int(fields[0] or 1), 0)
            contact, oscillation, ratio_raw = int(fields[1]), int(fields[2]), int(fields[3])
        except (TypeError, ValueError):
            continue
        if elapsed > MAX_WORKOUT_SECONDS:
            continue
        readings = (
            ("ground_contact_time", contact, "ms", contact != 65535 and 50 <= contact <= 1000),
            ("vertical_oscillation", oscillation, "mm", oscillation != 65535 and 1 <= oscillation <= 500),
            ("vertical_stride_ratio", ratio_raw / 10, "%", ratio_raw != 255 and 1 <= ratio_raw <= 1000),
        )
        for metric, value, unit, valid in readings:
            if valid:
                output.append(WorkoutMetricSample(
                    workout_id=workout_id, timestamp=start + timedelta(seconds=elapsed),
                    metric=metric, value=round(float(value), 2), unit=unit,
                ))
    return output


def _stream_laps(encoded):
    output = []
    for part in _stream_parts(encoded):
        if not part:
            continue
        fields = part.split(",")
        if len(fields) < 3:
            continue
        try:
            index, duration, distance = int(fields[0]), int(float(fields[1])), float(fields[2])
        except (TypeError, ValueError):
            continue
        if index >= 0 and duration >= 0 and distance >= 0:
            output.append(WorkoutLap(index=index, duration_seconds=duration, distance_meters=distance))
    return output


def _stream_pauses(encoded):
    output = []
    for part in _stream_parts(encoded):
        fields = part.split(",")
        if len(fields) < 2:
            continue
        started = ZeppParser._parse_datetime_value(fields[0])
        try:
            duration = int(fields[1])
        except (TypeError, ValueError):
            continue
        if started is not None and duration >= 0:
            output.append(WorkoutPause(started_at=ZeppParser._utc(started), duration_seconds=duration))
    return output
