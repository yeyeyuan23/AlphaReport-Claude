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


def is_daily_publication(paper, *, report_date: str | None = None) -> bool:
    """Use the same precise publication date rule before and after assessment."""
    from reportagent.services.document_metadata import has_precise_publication_date

    if not has_precise_publication_date(paper):
        return False
    published = (
        paper.get("published_date") if isinstance(paper, dict) else paper.published_date
    )
    if isinstance(published, str):
        try:
            published = datetime.fromisoformat(published)
        except ValueError:
            return False
    if not isinstance(published, datetime):
        return False
    # Database timestamps are UTC; SQLite returns them without tzinfo.
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    start, end, _, _ = get_daily_range(report_date)
    return start <= published <= end
