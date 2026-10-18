"""Public BigQuant catalog discovery, ordered by its own publication timestamp."""

from __future__ import annotations

import asyncio
from datetime import date
from urllib.parse import parse_qs, urlsplit

import httpx

from reportagent.models.schemas import UserCriteria
from reportagent.sources.base import SearchDiagnostics, SourceParseError
from reportagent.sources.content_fetch import MAX_HTML_BYTES
from reportagent.sources.date_window import in_criteria_window, source_date

API_URL = "https://bigquant.com/wiki/api"
COLLECTION_SLUG = "dLopDVBB53"
PAGE_SIZE = 100  # HTTP page size, not an overall candidate or download quota.


async def _request(client, endpoint, payload, diagnostics) -> dict:
    response = await client.post(f"{API_URL}/{endpoint}", json=payload)
    response.raise_for_status()
    if len(response.content) > MAX_HTML_BYTES:
        raise SourceParseError("catalog response exceeds metadata size limit")
    try:
        value = response.json()
    except ValueError:
        raise SourceParseError("catalog returned invalid JSON") from None
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise SourceParseError("catalog returned an unsuccessful response")
    diagnostics.successful_requests += 1
    return value


async def _collection_id(client, diagnostics) -> str:
    payload = await _request(
        client, "collections.info", {"id": COLLECTION_SLUG}, diagnostics
    )
    data = payload.get("data")
    identity = data.get("id") if isinstance(data, dict) else None
    if not isinstance(identity, str) or not identity:
        raise SourceParseError("catalog collection identity unavailable")
    return identity


def _announcement_date(value) -> date:
    day = source_date(value)
    if day is None:
        raise SourceParseError("catalog publication date unavailable")
    return day


def _records(rows: list, previous: date | None, diagnostics) -> list[tuple[dict, date]]:
    records = []
    for row in rows:
        try:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("id"), str)
                or not row["id"]
            ):
                raise SourceParseError("catalog document identity unavailable")
            stamp = _announcement_date(row.get("publishedAt"))
        except SourceParseError as error:
            diagnostics.record_error(error, "document metadata")
            continue
        if previous is not None and stamp > previous:
            raise SourceParseError(
                "catalog publishedAt DESC order violated; coverage incomplete"
            )
        previous = stamp
        records.append((row, stamp))
    if rows and not records:
        raise SourceParseError(
            "catalog has no usable dated records; coverage incomplete"
        )
    return records


def _next_offset(payload: dict, offset: int, count: int) -> int | None:
    if not count:
        return None
    pagination = payload.get("pagination")
    if not isinstance(pagination, dict) or "nextPath" not in pagination:
        raise SourceParseError("catalog pagination evidence unavailable")
    path = pagination["nextPath"]
    if path is None:
        return None
    try:
        next_offset = int(parse_qs(urlsplit(path).query)["offset"][0])
    except (KeyError, TypeError, ValueError):
        raise SourceParseError("catalog next-page offset invalid") from None
    if next_offset != offset + count:
        raise SourceParseError("catalog pagination did not advance consistently")
    return next_offset


async def _page(client, collection_id, offset, diagnostics) -> tuple[list, dict]:
    payload = await _request(
        client,
        "documents.list",
        {
            "collectionId": collection_id,
            "sort": "publishedAt",
            "direction": "DESC",
            "limit": PAGE_SIZE,
            "offset": offset,
        },
        diagnostics,
    )
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise SourceParseError("catalog missing document list")
    diagnostics.fetched += len(rows)
    return rows, payload


def _new_records(records, seen: set[str]) -> list:
    new = []
    for row, stamp in records:
        if row["id"] not in seen:
            seen.add(row["id"])
            new.append((row, stamp))
    if records and not new:
        raise SourceParseError("catalog pagination made no progress")
    return new


async def discover_catalog(
    client: httpx.AsyncClient,
    criteria: UserCriteria,
    diagnostics: SearchDiagnostics,
    rate_limit_seconds: float,
):
    """Yield only window candidates; stop at the proven descending time boundary."""
    collection_id = await _collection_id(client, diagnostics)
    seen: set[str] = set()
    offset, previous = 0, None
    while True:
        rows, payload = await _page(client, collection_id, offset, diagnostics)
        records = _records(rows, previous, diagnostics)
        for row, stamp in _new_records(records, seen):
            if criteria.date_basis != "announcement" or in_criteria_window(
                stamp, criteria
            ):
                yield row, stamp
        if not records:
            return
        previous = records[-1][1]
        if (
            criteria.date_basis == "announcement"
            and criteria.date_from
            and previous < source_date(criteria.date_from)
        ):
            return
        next_offset = _next_offset(payload, offset, len(rows))
        if next_offset is None:
            return
        offset = next_offset
        await asyncio.sleep(rate_limit_seconds)
