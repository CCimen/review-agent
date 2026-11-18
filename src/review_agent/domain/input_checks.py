"""Input checks shared by the operator and the decision use cases."""

from __future__ import annotations

from datetime import datetime, timezone

from .feedback import resolve_positive_int


class OperatorInputError(ValueError):
    """An operator request is ambiguous or unsafe to execute."""


def now(value: datetime | None) -> datetime:
    moment = value or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise OperatorInputError("now must include a timezone")
    return moment


def positive(value: int, *, field: str) -> int:
    try:
        return resolve_positive_int(value, field=field)
    except ValueError as exc:
        raise OperatorInputError(str(exc)) from exc
