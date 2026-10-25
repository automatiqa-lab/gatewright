"""Clock for the record. A demo database pins "today" so the war room looks like the mock-up.


"""

from __future__ import annotations

from datetime import UTC, date, datetime, time

_frozen: datetime | None = None


def now() -> datetime:
    """Current UTC time, or the pinned demo time."""
    return _frozen if _frozen is not None else datetime.now(UTC)


def today() -> date:
    return now().date()


def freeze(at: datetime | date | None) -> None:
    """Pin the clock (a date pins 06:30 UTC, the brief's issue time)."""
    global _frozen
    if at is None:
        _frozen = None
    elif isinstance(at, datetime):
        _frozen = at if at.tzinfo else at.replace(tzinfo=UTC)
    else:
        _frozen = datetime.combine(at, time(6, 30), tzinfo=UTC)


def is_frozen() -> bool:
    return _frozen is not None
