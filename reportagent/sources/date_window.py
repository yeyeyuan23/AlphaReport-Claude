"""Source date windows use UTC; legacy naive source timestamps mean UTC."""
from datetime import datetime, timezone


def utc_datetime(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def in_date_window(value: datetime | None, start: datetime | None, end: datetime | None) -> bool:
    if not start and not end:
        return True
    if value is None:
        return False
    stamp = utc_datetime(value)
    return (not start or stamp >= utc_datetime(start)) and (not end or stamp <= utc_datetime(end))
