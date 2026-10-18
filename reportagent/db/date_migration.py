"""Consolidate legacy source timestamps into one evidence-backed calendar date."""

import json

from reportagent.sources.date_window import source_date


def _metadata(value):
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def _legacy_date(row):
    info = _metadata(row["announcement_info_json"])
    if row["source"] in {"arxiv", "eastmoney", "bigquant"}:
        raw = info.get("listing_date") or info.get("evidence")
        day = source_date(raw)
        # Never relabel an old submission/cited-report date as a source release.
        return day, {**info, "precision": "day" if day else "unknown"}
    return source_date(row["published_date"]), _metadata(
        row["publication_date_info_json"]
    )


def migrate_source_dates(conn):
    columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(reports)")}
    if "announced_at" not in columns:
        return
    rows = (
        conn.exec_driver_sql(
            "SELECT id,source,published_date,publication_date_info_json,announcement_info_json FROM reports"
        )
        .mappings()
        .all()
    )
    for row in rows:
        day, info = _legacy_date(row)
        conn.exec_driver_sql(
            "UPDATE reports SET published_date=?,publication_date_info_json=? WHERE id=?",
            (
                day.isoformat() if day else None,
                json.dumps(info, ensure_ascii=False) if info else None,
                row["id"],
            ),
        )
    conn.exec_driver_sql("DROP INDEX IF EXISTS ix_reports_announced_at")
    conn.exec_driver_sql("ALTER TABLE reports DROP COLUMN announced_at")
    if "announcement_info_json" in columns:
        conn.exec_driver_sql("ALTER TABLE reports DROP COLUMN announcement_info_json")
