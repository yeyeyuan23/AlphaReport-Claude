"""Both web adapters cache only bounded, validated PDFs for the current row."""

import asyncio
from contextlib import contextmanager
import inspect
from pathlib import Path
from threading import get_ident
from types import SimpleNamespace

from fastapi import HTTPException
import httpx
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from reportagent.api import library, reports
from reportagent.models.database import Base, Report
from reportagent.sources import content_fetch
from reportagent.sources.base import SourceParseError
from reportagent.utils import config

PDF = b"%PDF-1.4\nvalid offline fixture\n%%EOF\n"
NEW_PDF = b"%PDF-1.5\nnewer offline fixture\n%%EOF\n"
MAX_BYTES = 128


class InterruptedStream(httpx.SyncByteStream, httpx.AsyncByteStream):
    def __init__(self, error):
        self.error = error

    def __iter__(self):
        yield b"%PDF-1.4\npartial"
        raise self.error("interrupted download")

    async def __aiter__(self):
        for chunk in self:
            yield chunk


@pytest.fixture(params=[library, reports], ids=["current", "legacy"])
def web_pdf(request, monkeypatch, tmp_path):
    adapter = request.param
    engine = create_engine(f"sqlite:///{tmp_path / 'reports.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(library, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        content_fetch,
        "get_config",
        lambda *args, **kwargs: {
            "max_size_mb": MAX_BYTES / 1024 / 1024,
            "download_timeout_seconds": 0.5,
            "max_attempts": 2,
            "retry_backoff_seconds": 0,
        },
    )
    requests = []
    responses = []

    def handler(request):
        requests.append(request)
        assert responses, "unexpected download (network is forbidden)"
        response = responses.pop(0)
        return response(request) if callable(response) else response

    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient
    sync_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: async_client(transport=transport, **kwargs),
    )

    # Cover the original synchronous adapter during the red run as well.
    def get(url, **kwargs):
        with sync_client(transport=transport) as client:
            return client.get(url, **kwargs)

    monkeypatch.setattr(httpx, "get", get)
    with factory() as db:
        row = Report(
            title="Offline paper",
            source="arxiv",
            source_url="https://arxiv.org/abs/quant-ph/9701001v1",
            source_item_id="arxiv:quant-ph/9701001",
            arxiv_id="quant-ph/9701001v1",
            content_hash="original-content",
        )
        db.add(row)
        db.commit()
        report_id = row.id

        def call():
            result = adapter.get_report_pdf(report_id, db)
            return asyncio.run(result) if inspect.isawaitable(result) else result

        yield {
            "adapter": adapter,
            "db": db,
            "factory": factory,
            "row": row,
            "id": report_id,
            "call": call,
            "responses": responses,
            "requests": requests,
            "directory": tmp_path / "data" / "pdfs",
        }
    engine.dispose()


def rejected(web_pdf, legacy_error=SourceParseError):
    error_type = HTTPException if web_pdf["adapter"] is library else legacy_error
    with pytest.raises(error_type) as caught:
        web_pdf["call"]()
    if error_type is HTTPException:
        assert caught.value.status_code == 404


def assert_not_cached(web_pdf):
    with web_pdf["factory"]() as db:
        assert db.get(Report, web_pdf["id"]).pdf_path is None
    assert not list(web_pdf["directory"].glob("*"))


@pytest.mark.parametrize(
    "kind", ["html", "empty", "declared_oversize", "streamed_oversize"]
)
def test_bad_success_response_is_not_cached_and_next_request_recovers(web_pdf, kind):
    if kind == "html":
        response = httpx.Response(200, content=b"<html>upstream error</html>")
    elif kind == "empty":
        response = httpx.Response(200, content=b"")
    elif kind == "declared_oversize":
        response = httpx.Response(
            200, content=PDF, headers={"content-length": str(MAX_BYTES + 1)}
        )
    else:
        response = httpx.Response(200, content=PDF + b"x" * MAX_BYTES)
        del response.headers["content-length"]
    web_pdf["responses"].append(response)
    rejected(web_pdf)
    assert_not_cached(web_pdf)
    web_pdf["responses"].append(httpx.Response(200, content=PDF))
    first = web_pdf["call"]()
    cached = web_pdf["call"]()
    assert Path(first.path).read_bytes() == PDF
    assert first.path == cached.path
    assert first.headers == cached.headers
    assert first.headers["content-type"] == "application/pdf"
    assert first.headers["content-disposition"] == (
        f'inline; filename="report_{web_pdf["id"]}.pdf"'
    )
    assert len(web_pdf["requests"]) == 2
    assert web_pdf["requests"][-1].url.path == "/pdf/quant-ph/9701001v1"


@pytest.mark.parametrize("error", [httpx.RemoteProtocolError, httpx.ReadError])
def test_interrupted_download_never_persists_partial_bytes(web_pdf, error):
    web_pdf["responses"].extend(
        httpx.Response(200, stream=InterruptedStream(error)) for _ in range(2)
    )
    rejected(web_pdf, error)
    assert_not_cached(web_pdf)
    # A later successful request must not reuse bytes from either failed attempt.
    web_pdf["responses"].clear()
    web_pdf["responses"].append(httpx.Response(200, content=PDF))
    assert Path(web_pdf["call"]().path).read_bytes() == PDF


@pytest.mark.parametrize("status", [404, 503])
def test_upstream_http_error_keeps_adapter_exception_contract(web_pdf, status):
    web_pdf["responses"].extend(httpx.Response(status) for _ in range(2))
    rejected(web_pdf, httpx.HTTPStatusError)
    assert_not_cached(web_pdf)


@pytest.mark.parametrize(
    "payload",
    [b"<html>old bad cache</html>", b""],
    ids=["html", "empty"],
)
def test_invalid_existing_cache_is_replaced_only_after_valid_download(web_pdf, payload):
    directory = web_pdf["directory"]
    directory.mkdir(parents=True)
    old_path = directory / "old.pdf"
    old_path.write_bytes(payload)
    web_pdf["row"].pdf_path = str(old_path)
    web_pdf["db"].commit()
    web_pdf["responses"].append(httpx.Response(200, content=PDF))
    response = web_pdf["call"]()
    assert Path(response.path).read_bytes() == PDF
    assert web_pdf["db"].get(Report, web_pdf["id"]).pdf_path == str(response.path)
    assert len(web_pdf["requests"]) == 1


def test_unreadable_existing_cache_is_not_served(web_pdf, monkeypatch):
    directory = web_pdf["directory"]
    directory.mkdir(parents=True)
    old_path = directory / "unreadable.pdf"
    old_path.write_bytes(PDF)
    web_pdf["row"].pdf_path = str(old_path)
    web_pdf["db"].commit()
    original_open = Path.open

    def open_file(path, *args, **kwargs):
        if path == old_path:
            raise OSError("cannot read cache")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    web_pdf["responses"].append(httpx.Response(200, content=NEW_PDF))
    response = web_pdf["call"]()
    assert Path(response.path).read_bytes() == NEW_PDF
    assert len(web_pdf["requests"]) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"source": "other"},
        {"source_url": "https://arxiv.org/abs/quant-ph/9701001v2"},
        {"source_item_id": "arxiv:new-source"},
        {"arxiv_id": "quant-ph/9701001v2"},
        {"content_hash": "new-evidence"},
        {"pdf_path": "newer.pdf"},
    ],
)
def test_changed_source_or_pdf_association_rejects_stale_download(web_pdf, changes):
    directory = web_pdf["directory"]
    directory.mkdir(parents=True)
    newer_path = directory / "newer.pdf"
    newer_path.write_bytes(NEW_PDF)
    changes = dict(changes)
    if "pdf_path" in changes:
        changes["pdf_path"] = str(newer_path)

    def newer_revision(request):
        with web_pdf["factory"]() as concurrent:
            current = concurrent.get(Report, web_pdf["id"])
            for field, value in changes.items():
                setattr(current, field, value)
            concurrent.commit()
        return httpx.Response(200, content=PDF)

    web_pdf["responses"].append(newer_revision)
    rejected(web_pdf)
    with web_pdf["factory"]() as db:
        row = db.get(Report, web_pdf["id"])
        assert all(getattr(row, field) == value for field, value in changes.items())
        assert row.pdf_path == changes.get("pdf_path")
    assert newer_path.read_bytes() == NEW_PDF
    assert list(directory.iterdir()) == [newer_path]


def test_atomic_promotion_failure_leaves_no_cache_and_next_request_recovers(
    web_pdf, monkeypatch
):
    original_replace = Path.replace

    def failed_replace(path, target):
        assert path.read_bytes() == PDF
        assert not Path(target).exists()
        raise OSError("atomic rename failed")

    monkeypatch.setattr(Path, "replace", failed_replace)
    web_pdf["responses"].append(httpx.Response(200, content=PDF))
    rejected(web_pdf, OSError)
    assert_not_cached(web_pdf)
    monkeypatch.setattr(Path, "replace", original_replace)
    web_pdf["responses"].append(httpx.Response(200, content=PDF))
    response = web_pdf["call"]()
    assert Path(response.path).read_bytes() == PDF
    assert list(web_pdf["directory"].iterdir()) == [Path(response.path)]


def test_commit_failure_removes_promoted_file_and_rolls_back_path(web_pdf, monkeypatch):
    original_commit = web_pdf["db"].commit

    def failed_commit():
        raise OSError("database commit failed")

    monkeypatch.setattr(web_pdf["db"], "commit", failed_commit)
    web_pdf["responses"].append(httpx.Response(200, content=PDF))
    rejected(web_pdf, OSError)
    assert_not_cached(web_pdf)
    monkeypatch.setattr(web_pdf["db"], "commit", original_commit)
    web_pdf["responses"].append(httpx.Response(200, content=PDF))
    assert Path(web_pdf["call"]().path).read_bytes() == PDF


def test_old_arxiv_identifier_can_be_derived_from_source_url(web_pdf):
    web_pdf["row"].arxiv_id = None
    web_pdf["db"].commit()
    web_pdf["responses"].append(httpx.Response(200, content=PDF))
    assert Path(web_pdf["call"]().path).read_bytes() == PDF
    assert web_pdf["requests"][0].url.path == "/pdf/quant-ph/9701001v1"


def test_adapter_signatures_stay_sync_and_async():
    assert not inspect.iscoroutinefunction(library.get_report_pdf)
    assert inspect.iscoroutinefunction(reports.get_report_pdf)


@pytest.mark.parametrize("web_pdf", [library], indirect=True, ids=["current"])
def test_sync_adapter_remains_callable_inside_running_event_loop(web_pdf):
    web_pdf["responses"].append(httpx.Response(200, content=PDF))
    caller_thread = get_ident()

    def check_db_thread(*args):
        assert get_ident() == caller_thread, "SQLAlchemy session crossed threads"

    event.listen(web_pdf["db"].bind, "before_cursor_execute", check_db_thread)

    async def call_from_loop():
        return library.get_report_pdf(web_pdf["id"], web_pdf["db"])

    response = asyncio.run(call_from_loop())
    assert Path(response.path).read_bytes() == PDF
    assert web_pdf["db"].get(Report, web_pdf["id"]).pdf_path == str(response.path)


@pytest.mark.parametrize("available", [True, False])
def test_local_only_report_validates_cache_without_upstream_download(
    web_pdf, available
):
    directory = web_pdf["directory"]
    directory.mkdir(parents=True)
    path = directory / "local.pdf"
    path.write_bytes(PDF if available else b"<html>bad legacy cache</html>")
    web_pdf["row"].source = "local_pdf"
    web_pdf["row"].source_url = None
    web_pdf["row"].pdf_path = str(path)
    web_pdf["db"].commit()
    if available:
        assert Path(web_pdf["call"]().path).read_bytes() == PDF
    else:
        with pytest.raises(HTTPException) as error:
            web_pdf["call"]()
        assert error.value.status_code == 404
    assert not web_pdf["requests"]


def test_deleted_report_during_download_is_not_recreated(web_pdf):
    def delete_row(request):
        with web_pdf["factory"]() as db:
            db.delete(db.get(Report, web_pdf["id"]))
            db.commit()
        return httpx.Response(200, content=PDF)

    web_pdf["responses"].append(delete_row)
    rejected(web_pdf)
    with web_pdf["factory"]() as db:
        assert db.get(Report, web_pdf["id"]) is None
    assert not list(web_pdf["directory"].glob("*"))


def test_invalid_existing_cache_is_retained_if_replacement_fails(web_pdf):
    directory = web_pdf["directory"]
    directory.mkdir(parents=True)
    path = directory / "old.pdf"
    old_bytes = b"<html>previous bad cache</html>"
    path.write_bytes(old_bytes)
    web_pdf["row"].pdf_path = str(path)
    web_pdf["db"].commit()
    web_pdf["responses"].append(httpx.Response(200, content=b"<html>new error</html>"))
    rejected(web_pdf)
    with web_pdf["factory"]() as db:
        assert db.get(Report, web_pdf["id"]).pdf_path == str(path)
    assert path.read_bytes() == old_bytes
    assert list(directory.iterdir()) == [path]


def test_transient_transfer_failure_retries_without_caching_partial_bytes(web_pdf):
    web_pdf["responses"].extend(
        [
            httpx.Response(200, stream=InterruptedStream(httpx.ReadError)),
            httpx.Response(200, content=PDF),
        ]
    )
    response = web_pdf["call"]()
    assert Path(response.path).read_bytes() == PDF
    assert len(web_pdf["requests"]) == 2


def test_partial_disk_write_cleans_up_and_leaves_cache_retryable(web_pdf, monkeypatch):
    from reportagent.services import report_pdf

    temporary_file = report_pdf.NamedTemporaryFile

    @contextmanager
    def failing_file(**kwargs):
        with temporary_file(**kwargs) as stream:

            def fail_write(data):
                stream.write(data[:8])
                raise OSError("disk full after partial write")

            yield SimpleNamespace(name=stream.name, write=fail_write)

    monkeypatch.setattr(report_pdf, "NamedTemporaryFile", failing_file)
    web_pdf["responses"].append(httpx.Response(200, content=PDF))
    rejected(web_pdf, OSError)
    assert_not_cached(web_pdf)
    monkeypatch.setattr(report_pdf, "NamedTemporaryFile", temporary_file)
    web_pdf["responses"].append(httpx.Response(200, content=PDF))
    assert Path(web_pdf["call"]().path).read_bytes() == PDF


def test_valid_large_local_cache_is_served_without_download_policy(
    web_pdf, monkeypatch
):
    directory = web_pdf["directory"]
    directory.mkdir(parents=True)
    path = directory / "large-local.pdf"
    payload = PDF + b"x" * MAX_BYTES
    path.write_bytes(payload)
    web_pdf["row"].source = "local_pdf"
    web_pdf["row"].source_url = None
    web_pdf["row"].pdf_path = str(path)
    web_pdf["db"].commit()
    response = web_pdf["call"]()
    assert Path(response.path).read_bytes() == payload
    assert not web_pdf["requests"]

    # A broken download setting must not block already imported, valid files.
    monkeypatch.setattr(content_fetch, "get_config", lambda *args, **kwargs: "invalid")
    assert Path(web_pdf["call"]().path).read_bytes() == payload


def test_post_commit_refresh_failure_cannot_remove_committed_pdf(web_pdf, monkeypatch):
    def failed_expire():
        raise RuntimeError("identity-map refresh failed after commit")

    monkeypatch.setattr(web_pdf["db"], "expire_all", failed_expire)
    web_pdf["responses"].append(httpx.Response(200, content=PDF))
    rejected(web_pdf, RuntimeError)
    with web_pdf["factory"]() as db:
        path = db.get(Report, web_pdf["id"]).pdf_path
        assert path is not None
        assert Path(path).read_bytes() == PDF
