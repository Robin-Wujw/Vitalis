"""Request-local work and payload budgets for descriptive health queries."""
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime
from functools import wraps
import json

from vitalis.intelligence.observation_statistics import ObservationBudgetExceeded


MAX_RECORDS = 50_000
MAX_SAMPLES = 2_000_000
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_RESULT_BYTES = 32 * 1024 * 1024


class HealthQueryBudgetExceeded(ValueError):
    """Actual saved data exceeds one synchronous query's resource budget."""


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError("unsupported health query result value")


def json_size(value, limit):
    size = 0
    for chunk in json.JSONEncoder(default=_json_default, ensure_ascii=False, separators=(",", ":")).iterencode(value):
        size += len(chunk.encode("utf-8"))
        if size > limit:
            raise HealthQueryBudgetExceeded("health query payload budget exceeded")
    return size


@dataclass
class HealthQueryBudget:
    records: int = 0
    samples: int = 0
    input_bytes: int = 0
    source_days: int = 0

    @property
    def remaining_records(self):
        return MAX_RECORDS - self.records

    @property
    def remaining_bytes(self):
        return MAX_INPUT_BYTES - self.input_bytes

    def record(self, value):
        self.records += 1
        if self.records > MAX_RECORDS:
            raise HealthQueryBudgetExceeded("health query record budget exceeded")
        self.input_bytes += json_size(value, self.remaining_bytes)

    def sample(self, row):
        self.samples += 1
        if self.samples > MAX_SAMPLES:
            raise HealthQueryBudgetExceeded("health query sample budget exceeded")
        return row

    def groups(self, groups):
        self.source_days += sum(len(group["days"]) for group in groups)
        if self.source_days > MAX_RECORDS:
            raise HealthQueryBudgetExceeded("health query source-day budget exceeded")
        return groups


_active_budget: ContextVar[HealthQueryBudget | None] = ContextVar("health_query_budget", default=None)


def current_budget() -> HealthQueryBudget:
    budget = _active_budget.get()
    if budget is None:
        raise RuntimeError("health read outside a query budget")
    return budget


def bounded_health_query(function):
    """Nested period sections share one budget; concurrent requests never do."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        existing = _active_budget.get()
        token = _active_budget.set(HealthQueryBudget()) if existing is None else None
        try:
            result = function(*args, **kwargs)
            if existing is None:
                json_size(result, MAX_RESULT_BYTES)
            return result
        except ObservationBudgetExceeded as exc:
            raise HealthQueryBudgetExceeded("health query source groups exceed budget") from exc
        finally:
            if token is not None:
                _active_budget.reset(token)
    return wrapped
