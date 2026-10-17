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


def publication_time(paper) -> datetime | None:
    """Prefer source announcement evidence; never use arXiv submission or a repost's original date."""
    from reportagent.services.document_metadata import has_precise_publication_date

    get = (
        paper.get if isinstance(paper, dict) else lambda key: getattr(paper, key, None)
    )
    stamp = get("announced_at")
    if not stamp:
        if get("source") in {
            "arxiv",
            "bigquant",
            "eastmoney",
        } or not has_precise_publication_date(paper):
            return None
        stamp = get("published_date")
    if isinstance(stamp, str):
        try:
            stamp = datetime.fromisoformat(stamp)
        except ValueError:
            return None
    if not isinstance(stamp, datetime):
        return None
    return (
        stamp.replace(tzinfo=timezone.utc)
        if stamp.tzinfo is None
        else stamp.astimezone(timezone.utc)
    )


def publication_column():
    """Coarse SQL window; publication_time applies source-specific evidence rules."""
    from sqlalchemy import func
    from reportagent.models.database import Report

    return func.coalesce(Report.announced_at, Report.published_date)


def is_publication_in_range(paper, start: datetime, end: datetime) -> bool:
    """Use one announcement/publication eligibility rule for collection and delivery."""
    stamp = publication_time(paper)
    if stamp is None:
        return False
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return start <= stamp < end


def is_daily_publication(paper, *, report_date: str | None = None) -> bool:
    """Use the same fixed publication window before and after assessment."""
    start, end, _, _ = get_daily_range(report_date)
    return is_publication_in_range(paper, start, end)
