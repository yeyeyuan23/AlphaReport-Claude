"""Publication windows ending at the Beijing collection cutoff, queried in UTC."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

REPORT_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _date_range(first: date, last: date) -> tuple[datetime, datetime, str, str]:
    start = datetime.combine(first, time.min, REPORT_TIMEZONE).astimezone(timezone.utc)
    end = datetime.combine(last, time.max, REPORT_TIMEZONE).astimezone(timezone.utc)
    return start, end, first.isoformat(), last.isoformat()


def get_calendar_day_range(
    report_date: str | None = None,
) -> tuple[datetime, datetime, str, str]:
    day = (
        date.fromisoformat(report_date)
        if report_date
        else datetime.now(REPORT_TIMEZONE).date()
    )
    return _date_range(day, day)


def _publication_range(day: date, days: int) -> tuple[datetime, datetime, str, str]:
    cutoff = datetime.combine(day, time(8), REPORT_TIMEZONE)
    start = cutoff - timedelta(days=days)
    return (
        start.astimezone(timezone.utc),
        cutoff.astimezone(timezone.utc),
        start.date().isoformat(),
        day.isoformat(),
    )


def get_daily_range(
    report_date: str | None = None,
) -> tuple[datetime, datetime, str, str]:
    """24 hours before the report day's 08:00 cutoff; labels identify the edition."""
    day = (
        date.fromisoformat(report_date)
        if report_date
        else datetime.now(REPORT_TIMEZONE).date()
    )
    start, end, _, label = _publication_range(day, 1)
    return start, end, label, label


def get_weekly_range(
    weeks_ago: int = 0, *, now: datetime | None = None
) -> tuple[datetime, datetime, str, str]:
    """168 hours before today's 08:00; backfill shifts the window by seven days."""
    if type(weeks_ago) is not int or weeks_ago < 0:
        raise ValueError("weeks_ago must be a nonnegative integer")
    day = (now or datetime.now(REPORT_TIMEZONE)).astimezone(REPORT_TIMEZONE).date()
    return _publication_range(day - timedelta(days=7 * weeks_ago), 7)


def format_publication_window(start: datetime, end: datetime) -> str:
    """Display the same half-open interval used by scoring and report queries."""
    return f"{start.astimezone(REPORT_TIMEZONE):%Y-%m-%d %H:%M} ~ {end.astimezone(REPORT_TIMEZONE):%Y-%m-%d %H:%M}（北京时间，含起点、不含截止点）"


def is_publication_in_range(paper, start: datetime, end: datetime) -> bool:
    """Reject partial/file dates and apply the shared half-open publication window."""
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
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return start <= published < end


def is_daily_publication(paper, *, report_date: str | None = None) -> bool:
    """Use the same fixed publication window before and after assessment."""
    start, end, _, _ = get_daily_range(report_date)
    return is_publication_in_range(paper, start, end)
