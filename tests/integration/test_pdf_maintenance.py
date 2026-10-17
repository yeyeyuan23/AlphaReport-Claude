"""Maintenance PDF parsing uses real files/rows and offline transfer boundaries."""

import json
from pathlib import Path
import sys

import fitz
import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.cli import parse_pdf
from reportagent.db import engine as db_engine
from reportagent.models.database import Base, Report
from reportagent.processors import mineru_parser
from reportagent.services import pdf_extraction
from reportagent.sources import content_fetch


@pytest.fixture
def maintenance(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'reports.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    path = tmp_path / "paper.pdf"
    with fitz.open() as doc:
        for page_number in (1, 2):
            page = doc.new_page()
            page.insert_text(
                (40, 40), f"Research page {page_number}: " + "offline material " * 12
            )
        doc.save(path)
    with factory() as db:
        row = Report(
            title="Research",
            abstract="Original abstract",
            source="arxiv",
            arxiv_id="quant-ph/9701001v1",
            source_url="https://arxiv.org/abs/quant-ph/9701001v1",
            source_item_id="arxiv:quant-ph/9701001",
            content_hash="stable",
            full_text="old full text",
            has_full_text=True,
            pdf_path=str(path),
            tables_json='[{"old": true}]',
            equations_json='[{"old": true}]',
            summary="old summary",
            analysis_json='{"old": true}',
            evaluation_json='{"old": true}',
            knowledge_card_json='{"old": true}',
            selection_score=88,
            selection_reason="Screened abstract",
            selection_provenance_json='{"current": true}',
            is_selected=True,
        )
        db.add(row)
        db.commit()
        rid = row.id
    monkeypatch.setattr(db_engine, "init_db", lambda: None)
    monkeypatch.setattr(db_engine, "get_session_factory", lambda: factory)
    monkeypatch.setattr(content_fetch, "PDF_DIRECTORY", tmp_path / "cache")
    monkeypatch.setattr(
        content_fetch,
        "get_config",
        lambda *a, **kw: {
            "max_attempts": 1,
            "max_size_mb": 1,
            "retry_backoff_seconds": 0,
        },
    )

    def unavailable(*args):
        raise ImportError("offline: MinerU unavailable")

    monkeypatch.setattr(mineru_parser, "_run_mineru_parse", unavailable)

    def run(*args):
        monkeypatch.setattr(sys, "argv", ["parse_pdf", *map(str, args)])
        return parse_pdf.main()

    yield factory, rid, path, run
    engine.dispose()


def test_save_reloads_attached_row_and_replaces_entire_bundle(maintenance):
    factory, rid, path, run = maintenance
    run("--report-id", rid, "--save")
    with factory() as db:
        row = db.get(Report, rid)
        assert "Research page 1" in row.full_text
        assert row.has_full_text
        assert row.pdf_path == str(path)
        assert json.loads(row.tables_json) == []
        assert json.loads(row.equations_json) == []
        for field in (
            "summary",
            "analysis_json",
            "evaluation_json",
            "knowledge_card_json",
        ):
            assert getattr(row, field) is None
        assert row.selection_score == 88
        assert row.selection_reason == "Screened abstract"
        assert row.selection_provenance_json == '{"current": true}'
        assert row.is_selected


@pytest.mark.parametrize(
    "changes",
    [
        {"full_text": "concurrently replaced"},
        {"abstract": "new abstract"},
        {"pdf_path": "/newer.pdf"},
        {"analysis_json": '{"newer": true}'},
    ],
)
def test_concurrent_changes_reject_stale_parse(maintenance, monkeypatch, changes):
    factory, rid, path, run = maintenance
    original = parse_pdf.extract_formulas_mineru

    def concurrent(pdf_path):
        result = original(pdf_path)
        with factory() as db:
            row = db.get(Report, rid)
            for key, value in changes.items():
                setattr(row, key, value)
            db.commit()
        return result

    monkeypatch.setattr(parse_pdf, "extract_formulas_mineru", concurrent)
    with pytest.raises(SystemExit) as caught:
        run("--report-id", rid, "--save")
    assert caught.value.code == 1
    with factory() as db:
        row = db.get(Report, rid)
        for key, value in changes.items():
            assert getattr(row, key) == value
        assert row.tables_json == '[{"old": true}]'
        assert row.summary == "old summary"


@pytest.mark.parametrize("failure", ["empty", "exception"])
def test_failed_parse_preserves_old_material(maintenance, monkeypatch, failure):
    factory, rid, path, run = maintenance

    def fail(pdf_path):
        if failure == "exception":
            raise ValueError("broken parser")
        return [], [], ""

    monkeypatch.setattr(parse_pdf, "extract_formulas_mineru", fail)
    with pytest.raises(SystemExit) as caught:
        run("--report-id", rid, "--save")
    assert caught.value.code == 1
    with factory() as db:
        row = db.get(Report, rid)
        assert row.full_text == "old full text"
        assert row.pdf_path == str(path)
        assert row.summary == "old summary"


@pytest.mark.parametrize(
    "args",
    [("--report-id", "0"), ("--report-id", "-1"), ("--pdf", "local.pdf", "--save")],
)
def test_invalid_save_target_fails_at_argument_validation(maintenance, args):
    *_, run = maintenance
    with pytest.raises(SystemExit) as caught:
        run(*args)
    assert caught.value.code == 2


@pytest.mark.parametrize("kind", ["valid", "html", "oversize", "empty_text"])
def test_missing_pdf_uses_bounded_validated_atomic_cache(
    maintenance, monkeypatch, kind
):
    factory, rid, path, run = maintenance
    original_pdf = path.read_bytes()
    if kind == "valid":
        payload = original_pdf
    elif kind == "html":
        payload = b"<html>upstream error</html>"
    elif kind == "oversize":
        payload = original_pdf + b"x" * 1024 * 1024
    else:
        with fitz.open() as doc:
            doc.new_page()
            payload = doc.tobytes()
    path.write_bytes(b"<html>old cache error</html>")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=payload)

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client(transport=httpx.MockTransport(handler), **kw),
    )
    if kind == "valid":
        run("--report-id", rid, "--save")
        with factory() as db:
            row = db.get(Report, rid)
            assert Path(row.pdf_path).read_bytes() == original_pdf
            assert "Research page 1" in row.full_text
            assert json.loads(row.tables_json) == []
            assert row.pdf_path != str(path)
    else:
        with pytest.raises(SystemExit) as caught:
            run("--report-id", rid, "--save")
        assert caught.value.code == 1
        with factory() as db:
            row = db.get(Report, rid)
            assert row.pdf_path == str(path)
            assert row.full_text == "old full text"
            assert row.summary == "old summary"
        assert not list((path.parent / "cache").glob("*"))
    assert requests[0].url.path == "/pdf/quant-ph/9701001v1"
    assert path.read_bytes() == b"<html>old cache error</html>"


def test_formula_extraction_uses_public_mineru_parser(maintenance, monkeypatch):
    _, _, path, _ = maintenance
    from reportagent.processors.mineru_parser import ParsedContent

    monkeypatch.setattr(
        mineru_parser.MinerUParser,
        "parse",
        lambda self, p: ParsedContent(
            text="public parser material", tables=[{"table_body": "table"}]
        ),
    )
    tables, equations, text = pdf_extraction.extract_formulas_mineru(str(path))
    assert tables == [{"table_body": "table"}]
    assert equations == []
    assert "public parser material" in text


def test_empty_mineru_result_falls_back_to_real_pdf_text(maintenance, monkeypatch):
    _, _, path, _ = maintenance
    monkeypatch.setattr(mineru_parser, "_run_mineru_parse", lambda *args: [])
    tables, equations, text = pdf_extraction.extract_formulas_mineru(str(path))
    assert "Research page 1" in text
    assert tables == equations == []


def test_cached_pdf_replaced_during_parse_cannot_save_stale_text(
    maintenance, monkeypatch
):
    factory, rid, path, run = maintenance
    original = parse_pdf.extract_formulas_mineru

    def replaced(pdf_path):
        result = original(pdf_path)
        with fitz.open() as document:
            document.new_page().insert_text((40, 40), "different evidence")
            document.save(str(path) + ".new")
        Path(str(path) + ".new").replace(path)
        return result

    monkeypatch.setattr(parse_pdf, "extract_formulas_mineru", replaced)
    with pytest.raises(SystemExit) as caught:
        run("--report-id", rid, "--save")
    assert caught.value.code == 1
    with factory() as db:
        row = db.get(Report, rid)
        assert row.full_text == "old full text"
        assert row.summary == "old summary"
