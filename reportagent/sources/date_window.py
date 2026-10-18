"""Source calendar dates are compared inclusively, without inferred clock times."""

from datetime import date, datetime


def source_date(value) -> date | None:
    """Keep the calendar date supplied by a source; never shift its timezone."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            pass
    return None


def in_date_window(value, start, end) -> bool:
    if not start and not end:
        return True
    day = source_date(value)
    if day is None:
        return False
    first, last = source_date(start), source_date(end)
    return (first is None or first <= day) and (last is None or day <= last)


def in_criteria_window(value, criteria) -> bool:
    return in_date_window(value, criteria.date_from, criteria.date_to)


def candidate_in_window(result, criteria) -> bool:
    return in_criteria_window(result.published_date, criteria)
