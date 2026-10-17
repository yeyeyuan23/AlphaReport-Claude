"""Source date windows use UTC; legacy naive source timestamps mean UTC."""

from datetime import datetime, timezone

from reportagent.models.schemas import SearchResult, UserCriteria


def utc_datetime(value: datetime) -> datetime:
    return (
        value.astimezone(timezone.utc)
        if value.tzinfo
        else value.replace(tzinfo=timezone.utc)
    )


def in_date_window(
    value: datetime | None,
    start: datetime | None,
    end: datetime | None,
    *,
    end_exclusive: bool = False,
) -> bool:
    if not start and not end:
        return True
    if value is None:
        return False
    stamp = utc_datetime(value)
    return (not start or stamp >= utc_datetime(start)) and (
        not end
        or (stamp < utc_datetime(end) if end_exclusive else stamp <= utc_datetime(end))
    )


def in_criteria_window(value: datetime | None, criteria: UserCriteria) -> bool:
    return in_date_window(
        value,
        criteria.date_from,
        criteria.date_to,
        end_exclusive=criteria.date_to_exclusive,
    )


def candidate_in_window(result: SearchResult, criteria: UserCriteria) -> bool:
    """One source-independent date policy, before abstracts/PDFs or model calls."""
    value = (
        result.announced_at
        if criteria.date_basis == "announcement"
        else result.published_date
    )
    if criteria.date_basis == "announcement" and value is None:
        return False
    return in_criteria_window(value, criteria)
