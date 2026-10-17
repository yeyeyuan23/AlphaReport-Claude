"""Validated PDF cache shared by report reading and maintenance."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

import httpx
from sqlalchemy import update

from reportagent.models.database import Report
from reportagent.sources import content_fetch
from reportagent.sources.base import SourceParseError


def pdf_snapshot(report):
    return {
        field: getattr(report, field)
        for field in (
            "id",
            "source",
            "source_url",
            "source_item_id",
            "arxiv_id",
            "content_hash",
            "pdf_path",
        )
    }


def cached_pdf_path(report):
    """An old cached error page or unreadable file is a cache miss."""
    if not report.pdf_path:
        return None
    path = Path(report.pdf_path)
    try:
        if not path.is_file():
            return None
        with path.open("rb") as stream:
            content_fetch.validate_pdf_header(
                stream.read(1024), source=report.source, paper_id=str(report.id)
            )
    except (OSError, SourceParseError):
        return None
    return str(path)


async def download_pdf(snapshot, policy):
    # Historical slash-style arXiv identifiers remain valid here.
    arxiv_id = snapshot["arxiv_id"] or snapshot["source_url"].split("/abs/")[-1]
    try:
        async with httpx.AsyncClient() as client:
            data = await content_fetch.bounded_download(
                client,
                f"https://arxiv.org/pdf/{arxiv_id}",
                policy.max_bytes,
                policy=policy,
                source=snapshot["source"],
                paper_id=arxiv_id,
            )
    except content_fetch.PDFTransferError as error:
        # HTTP adapters need the underlying network failure to choose a response.
        if isinstance(error.__cause__, (httpx.HTTPError, TimeoutError)):
            raise error.__cause__ from error
        raise
    content_fetch.validate_pdf_header(
        data, source=snapshot["source"], paper_id=arxiv_id
    )
    return data


def download_pdf_sync(snapshot, policy):
    """Bridge only network work; a caller's SQLAlchemy session stays on its thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(download_pdf(snapshot, policy))
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(download_pdf(snapshot, policy))).result()


def save_pdf(db, snapshot, data, directory, *, values=None):
    """Compare-and-set the row, then atomically promote a unique complete file."""
    target = directory / f"report-{snapshot['id']}-{uuid4().hex}.pdf"
    temporary = None
    try:
        # The conditional write checks current database values, not the ORM's
        # potentially stale identity map, and locks the row through promotion.
        with db.no_autoflush:
            result = db.execute(
                update(Report)
                .where(
                    *(
                        getattr(Report, field) == value
                        for field, value in snapshot.items()
                    )
                )
                .values(**{**(values or {}), "pdf_path": str(target)})
                .execution_options(synchronize_session=False)
            )
        if result.rowcount != 1:
            raise SourceParseError("Report source or PDF changed during download")
        directory.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            dir=directory, suffix=".partial", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        temporary.replace(target)
        db.commit()
    except BaseException:
        db.rollback()
        target.unlink(missing_ok=True)
        raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    db.expire_all()
    return str(target)
