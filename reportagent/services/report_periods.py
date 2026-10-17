"""Report calendar boundaries in Beijing time, expressed as UTC for the database."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

REPORT_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _date_range(first: date, last: date) -> tuple[datetime, datetime, str, str]:
    start = datetime.combine(first, time.min, REPORT_TIMEZONE).astimezone(timezone.utc)
    end = datetime.combine(last, time.max, REPORT_TIMEZONE).astimezone(timezone.utc)
    return start, end, first.isoformat(), last.isoformat()


def get_daily_range(
    report_date: str | None = None,
) -> tuple[datetime, datetime, str, str]:
    day = (
        date.fromisoformat(report_date)
        if report_date
        else datetime.now(REPORT_TIMEZONE).date()
    )
    return _date_range(day, day)


def get_weekly_range(
    weeks_ago: int = 0, *, now: datetime | None = None
) -> tuple[datetime, datetime, str, str]:
    """Include today and six preceding dates; backfill shifts by seven days."""
    if type(weeks_ago) is not int or weeks_ago < 0:
        raise ValueError("weeks_ago must be a nonnegative integer")
    day = (now or datetime.now(REPORT_TIMEZONE)).astimezone(REPORT_TIMEZONE).date()
    last = day - timedelta(days=7 * weeks_ago)
    return _date_range(last - timedelta(days=6), last)
