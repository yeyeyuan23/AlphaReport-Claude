"""Startup imports real local PDFs once, preserving retries and service availability."""

import asyncio
import sys
from types import ModuleType, SimpleNamespace

import fitz
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.models.database import Base, Report


def write_pdf(path, title="Portfolio risk model", extra=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open() as document:
        page = document.new_page()
        page.insert_textbox(
            fitz.Rect(40, 40, 550, 750),
            title
            + "\nAbstract\n"
            + (
                "We model financial stock returns and portfolio risk using quantitative factors. "
                * 6
            )
            + "\n1 Introduction\n"
            + "This section discusses portfolio construction and financial factors. "
            + extra,
        )
        document.set_metadata({"title": title, "author": "Fixture Author"})
        document.save(path)


@pytest.fixture
def importer(tmp_path, monkeypatch):
    from reportagent.services import local_pdf_import as module
    from reportagent.sources import local_pdf

    engine = create_engine(f"sqlite:///{tmp_path / 'library.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    root = tmp_path / "pdf_library"
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(module, "get_db_path", lambda: tmp_path / "library.db")
    monkeypatch.setattr(module, "init_db", lambda: None)
    monkeypatch.setattr(module, "get_session_factory", lambda: factory)
    config = {"enabled": True, "import_on_startup": True, "library_path": "pdf_library"}
    monkeypatch.setattr(
        module,
        "get_config",
        lambda *keys, default=None: config
        if keys == ("sources", "local_pdf")
        else default,
    )
    monkeypatch.setattr(local_pdf, "_get_mineru_parser", lambda: None)
    monkeypatch.setattr(local_pdf, "_get_docling_parser", lambda: None)
    calls = []
    fail = []

    class Client:
        async def chat_json(self, messages):
            calls.append(messages)
            if fail:
                raise RuntimeError("model temporarily unavailable")
            return {
                "markets": [],
                "asset_classes": ["stock"],
                "frequencies": [],
                "topics": ["risk_model"],
                "custom_tags": [],
                "confidence": 0.9,
                "evidence": {
                    "asset_classes.stock": {
                        "source": "material",
                        "quote": "stock returns",
                        "reason": "Stock asset",
                    },
                    "topics.risk_model": {
                        "source": "material",
                        "quote": "portfolio risk",
                        "reason": "Risk model",
                    },
                },
            }

    monkeypatch.setattr(module, "LLMClient", Client)
    yield SimpleNamespace(
        module=module, factory=factory, root=root, calls=calls, fail=fail, config=config
    )
    engine.dispose()


def test_startup_launches_import_in_background_and_cancels_on_shutdown(monkeypatch):
    import reportagent.main as main
    from reportagent.services import scheduler

    started = asyncio.Event()
    cancelled = []

    async def import_library():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    fake = ModuleType("reportagent.services.local_pdf_import")
    fake.import_pdf_library = import_library
    monkeypatch.setitem(sys.modules, fake.__name__, fake)
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(scheduler, "start_scheduler", lambda: None)
    monkeypatch.setattr(scheduler, "stop_scheduler", lambda: None)

    async def run():
        async with main.lifespan(SimpleNamespace()):
            await asyncio.sleep(0.01)
            assert started.is_set(), (
                "Service startup must trigger PDF import without awaiting its completion"
            )
        assert cancelled == [True]

    asyncio.run(run())


def test_standalone_bot_starts_import_without_delaying_websocket(monkeypatch):
    import reportagent.bot.feishu as bot
    import threading

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    observations = []

    async def import_library():
        started.set()
        try:
            await asyncio.to_thread(release.wait)
        finally:
            finished.set()

    fake = ModuleType("reportagent.services.local_pdf_import")
    fake.import_pdf_library = import_library
    monkeypatch.setitem(sys.modules, fake.__name__, fake)
    monkeypatch.setattr(bot, "APP_ID", "test-app")
    monkeypatch.setattr(bot, "APP_SECRET", "test-secret")
    monkeypatch.setattr(bot, "init_db", lambda: None)
    monkeypatch.setattr(bot, "http_callback_enabled", lambda: False)

    class WebSocket:
        def __init__(self, **kwargs):
            pass

        def start(self):
            observations.append((started.wait(timeout=1), finished.is_set()))
            raise KeyboardInterrupt

    monkeypatch.setattr(bot, "WSClient", WebSocket)
    try:
        bot.main()
    finally:
        release.set()
        if started.is_set():
            assert finished.wait(timeout=2)
    assert observations == [(True, False)], (
        "Bot startup must initiate import without blocking WebSocket"
    )


def test_recursive_import_only_classifies_new_or_changed_files(importer):
    a = importer.root / "first.pdf"
    b = importer.root / "nested" / "second.PDF"
    write_pdf(a)
    write_pdf(b, "Stock factor prediction")
    result = asyncio.run(importer.module.import_pdf_library())
    assert (result["scanned"], result["added"], result["failed"]) == (2, 2, 0)
    with importer.factory() as session:
        report = session.query(Report).filter_by(title="Portfolio risk model").one()
        assert report.has_full_text and "portfolio risk" in report.full_text
        assert report.classification_method == "llm"
        assert report.analysis_json is None
        report.analysis_json = '{"saved": true}'
        session.commit()
    second = asyncio.run(importer.module.import_pdf_library())
    assert second["unchanged"] == 2
    assert len(importer.calls) == 2
    with importer.factory() as session:
        assert (
            session.query(Report)
            .filter_by(title="Portfolio risk model")
            .one()
            .analysis_json
            == '{"saved": true}'
        )
    a.unlink()
    write_pdf(a, extra="The revised paper adds a new factor experiment.")
    third = asyncio.run(importer.module.import_pdf_library())
    assert (third["updated"], third["unchanged"]) == (1, 1)
    assert len(importer.calls) == 3
    with importer.factory() as session:
        assert session.query(Report).count() == 2
        assert (
            session.query(Report)
            .filter_by(title="Portfolio risk model")
            .one()
            .analysis_json
            is None
        )


def test_more_than_default_collection_limit_is_imported(importer):
    for i in range(22):
        write_pdf(importer.root / f"{i}.pdf", title=f"Portfolio risk model {i}")
    result = asyncio.run(importer.module.import_pdf_library())
    assert result["added"] == 22
    assert len(importer.calls) == 22


def test_date_precision_and_evidence_survive_import_and_library_queries(importer):
    import json
    from reportagent.cli.db_query import _report_to_dict

    write_pdf(
        importer.root / "paper.pdf",
        extra="\nPublished as a conference paper at ICLR 2025",
    )
    result = asyncio.run(importer.module.import_pdf_library())
    assert result["added"] == 1
    with importer.factory() as session:
        row = session.query(Report).one()
        assert row.published_date is None
        info = json.loads(row.publication_date_info_json)
        assert info["year"] == 2025 and info["precision"] == "year"
        assert "ICLR 2025" in info["evidence"]
        assert _report_to_dict(row)["publication_date_info"]["year"] == 2025


def test_bad_pdf_does_not_block_other_files_and_can_be_retried(importer):
    bad = importer.root / "a.pdf"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a PDF")
    write_pdf(importer.root / "b.pdf")
    first = asyncio.run(importer.module.import_pdf_library())
    assert (first["failed"], first["added"]) == (1, 1)
    bad.unlink()
    write_pdf(bad, title="New financial factor paper")
    second = asyncio.run(importer.module.import_pdf_library())
    assert (second["added"], second["unchanged"], second["failed"]) == (1, 1, 0)


def test_classification_failure_stores_fallback_but_retries_next_startup(importer):
    write_pdf(importer.root / "paper.pdf")
    importer.fail.append(True)
    first = asyncio.run(importer.module.import_pdf_library())
    assert first["status"] == "partial"
    with importer.factory() as session:
        assert session.query(Report).one().classification_method == "rule"
    importer.fail.clear()
    second = asyncio.run(importer.module.import_pdf_library())
    assert second["updated"] == 1 and len(importer.calls) == 2
    with importer.factory() as session:
        assert session.query(Report).one().classification_method == "llm"


def test_deleted_database_row_is_reimported_even_if_file_is_unchanged(importer):
    write_pdf(importer.root / "paper.pdf")
    asyncio.run(importer.module.import_pdf_library())
    with importer.factory() as session:
        session.query(Report).delete()
        session.commit()
    result = asyncio.run(importer.module.import_pdf_library())
    assert result["added"] == 1 and len(importer.calls) == 2


def test_reused_database_id_does_not_hide_a_deleted_import(importer):
    write_pdf(importer.root / "paper.pdf")
    asyncio.run(importer.module.import_pdf_library())
    with importer.factory() as session:
        old_id = session.query(Report).one().id
        session.query(Report).delete()
        session.add(
            Report(title="Unrelated paper", source="arxiv", content_hash="unrelated")
        )
        session.commit()
        assert session.query(Report).one().id == old_id
    result = asyncio.run(importer.module.import_pdf_library())
    assert result["added"] == 1 and result["unchanged"] == 0
    with importer.factory() as session:
        assert {r.title for r in session.query(Report)} == {
            "Portfolio risk model",
            "Unrelated paper",
        }


def test_disabled_import_does_not_create_library_or_call_model(importer):
    importer.config["import_on_startup"] = False
    result = asyncio.run(importer.module.import_pdf_library())
    assert result["status"] == "disabled"
    assert not importer.root.exists() and not importer.calls


def test_missing_library_is_created_and_empty_scan_is_successful(importer):
    result = asyncio.run(importer.module.import_pdf_library())
    assert result["status"] == "ok" and result["scanned"] == 0
    assert importer.root.is_dir() and not importer.calls


def test_simultaneous_startups_do_not_duplicate_classification(importer):
    write_pdf(importer.root / "paper.pdf")
    entered = asyncio.Event()
    release = asyncio.Event()

    class Client:
        async def chat_json(self, messages):
            importer.calls.append(messages)
            entered.set()
            await release.wait()
            return {
                "markets": ["global"],
                "asset_classes": ["stock"],
                "frequencies": [],
                "topics": ["risk_model"],
                "custom_tags": [],
                "confidence": 0.9,
            }

    importer.module.LLMClient = Client

    async def run():
        first = asyncio.create_task(importer.module.import_pdf_library())
        await asyncio.wait_for(entered.wait(), timeout=2)
        second = await importer.module.import_pdf_library()
        assert second["status"] == "already_running"
        release.set()
        return await first

    result = asyncio.run(run())
    assert result["added"] == 1 and len(importer.calls) == 1
