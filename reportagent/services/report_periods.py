"""Shared source-date ranges; job trigger times do not change date eligibility."""

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


def get_daily_range(report_date: str | None = None) -> tuple[date, date, str, str]:
    """Yesterday through today, inclusive, matching source date-list requests."""
    day = (
        date.fromisoformat(report_date)
        if report_date
        else datetime.now(REPORT_TIMEZONE).date()
    )
    return day - timedelta(days=1), day, day.isoformat(), day.isoformat()


def get_weekly_range(
    weeks_ago: int = 0, *, now: datetime | None = None
) -> tuple[date, date, str, str]:
    """Seven calendar dates including the report date."""
    if type(weeks_ago) is not int or weeks_ago < 0:
        raise ValueError("weeks_ago must be a nonnegative integer")
    day = (now or datetime.now(REPORT_TIMEZONE)).astimezone(
        REPORT_TIMEZONE
    ).date() - timedelta(days=7 * weeks_ago)
    first = day - timedelta(days=6)
    return first, day, first.isoformat(), day.isoformat()


def format_publication_window(start, end) -> str:
    return f"{start:%Y-%m-%d} ~ {end:%Y-%m-%d}（来源日期，含起止日期）"


def publication_column():
    from reportagent.models.database import Report

    return Report.published_date


def is_publication_in_range(paper, start, end) -> bool:
    from reportagent.services.document_metadata import has_precise_publication_date
    from reportagent.sources.date_window import in_date_window

    get = (
        paper.get if isinstance(paper, dict) else lambda key: getattr(paper, key, None)
    )
    return has_precise_publication_date(paper) and in_date_window(
        get("published_date"), start, end
    )


def is_daily_publication(paper, *, report_date: str | None = None) -> bool:
    start, end, _, _ = get_daily_range(report_date)
    return is_publication_in_range(paper, start, end)
