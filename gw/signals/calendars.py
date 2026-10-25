"""Business days per party country.and
: the `holidays` library plus `policies.calendars.extra_holidays`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import cache
from typing import Any

import holidays as hol


@cache
def _country_holidays(country: str, years: tuple[int, ...]) -> frozenset[date]:
    try:
        return frozenset(hol.country_holidays(country, years=list(years)).keys())
    except (NotImplementedError, KeyError):
        return frozenset()


def is_business_day(d: date, country: str, extra: dict[str, list[str]] | None = None) -> bool:
    if d.weekday() >= 5:
        return False
    if d in _country_holidays(country, (d.year,)):
        return False
    return not (extra and d.isoformat() in (extra.get(country) or []))


def business_days_between(start: date | datetime, end: date | datetime, country: str,
                          extra: dict[str, list[str]] | None = None) -> int:
    """Business days after `start` up to and including `end` (0 when end <= start)."""
    s = start.date() if isinstance(start, datetime) else start
    e = end.date() if isinstance(end, datetime) else end
    n = 0
    d = s + timedelta(days=1)
    while d <= e:
        if is_business_day(d, country, extra):
            n += 1
        d += timedelta(days=1)
    return n


def add_business_days(start: date, n: int, country: str, extra: dict[str, list[str]] | None = None) -> date:
    d = start
    step = 1 if n >= 0 else -1
    left = abs(n)
    while left:
        d += timedelta(days=step)
        if is_business_day(d, country, extra):
            left -= 1
    return d


def extra_from(policies: Any) -> dict[str, list[str]]:
    return dict(policies.get("calendars.extra_holidays") or {})
