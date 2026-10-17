"""The maintained HTTP reader delegates to shared, evidence-aware workflows."""

from datetime import datetime
import inspect
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from reportagent.api import library
from reportagent.db.repository import ReportRepository
from reportagent.models.database import Base, Report, Tag
from reportagent.services import report_views
from tests.unit.test_knowledge_card_evidence import reviewed_paper

BASE_FIELDS = {
    "id",
    "title",
    "authors",
    "abstract",
    "source",
    "source_url",
    "doi",
    "arxiv_id",
    "published_date",
    "has_full_text",
    "pdf_path",
    "markets",
    "asset_classes",
    "frequencies",
    "topics",
    "custom_tags",
    "content_hash",
    "created_at",
    "updated_at",
}
CURRENT_FIELDS = {
    "publication_date_info",
    "classification_provenance",
    "summary",
    "selection_relevance",
    "selection_direction",
    "selection_reason",
    "evaluation",
}
DETAIL_FIELDS = {"full_text", "summary", "tables_json", "equations_json", "analysis"}


@pytest.fixture(params=[library], ids=["current"])
def reader(request):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    p = reviewed_paper()
    with factory() as db:
        first = Report(
            **p,
            authors='["First Author", "Second Author"]',
            source_url="https://arxiv.org/abs/2601.00001v1",
            doi="10.1000/contract",
            published_date=datetime(2026, 10, 10),
            publication_date_info_json='{"precision": "day", "evidence": "10 October 2026"}',
            pdf_path="data/pdfs/fixture.pdf",
            content_hash="read-1",
            markets="us",
            asset_classes="equity",
            frequencies="daily",
            topics="portfolio",
            classification_provenance_json='{"method": "llm", "evidence": ["portfolio"]}',
            selection_direction="portfolio optimization",
            summary="Saved summary",
            tables_json='[{"caption": "Results"}]',
            equations_json='[{"latex": "x=y"}]',
            analysis_json='{"summary": "Unsigned historical analysis"}',
            created_at=datetime(2026, 10, 12),
            updated_at=datetime(2026, 10, 13),
            tags=[
                Tag(dimension="custom", value="reading-list"),
                Tag(dimension="topic", value="portfolio"),
            ],
        )
        db.add(first)
        for (
            rid,
            title,
            market,
            asset,
            frequency,
            topic,
            source,
            full,
            date,
            abstract,
        ) in [
            (
                2,
                "Alpha signal",
                "cn",
                "equity",
                "week",
                "momentum",
                "local_pdf",
                False,
                9,
                "Another allocation study",
            ),
            (
                3,
                "Zulu rates",
                "us",
                "bond",
                "daily",
                "macro",
                "arxiv",
                True,
                12,
                "A rates overview",
            ),
            (
                4,
                "Beta portfolio",
                "cn",
                "bond",
                "minute",
                "volatility",
                "eastmoney",
                False,
                None,
                None,
            ),
        ]:
            db.add(
                Report(
                    id=rid,
                    title=title,
                    abstract=abstract,
                    source=source,
                    content_hash=f"read-{rid}",
                    markets=market,
                    asset_classes=asset,
                    frequencies=frequency,
                    topics=topic,
                    has_full_text=full,
                    published_date=datetime(2026, 10, date) if date else None,
                    created_at=datetime(2026, 10, 13 - rid),
                )
            )
        db.commit()

    commits = []
    event.listen(factory, "after_commit", lambda session: commits.append(True))

    def database():
        with factory() as db:
            yield db

    # Mount each real router separately: no scheduler, real model, or unrelated API.
    app = FastAPI()
    app.include_router(request.param.router)
    app.dependency_overrides[request.param.get_db] = database
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client,
            factory=factory,
            adapter=request.param,
            current=request.param is library,
            commits=commits,
        )
    engine.dispose()


def get_data(reader, path="", **params):
    result = reader.client.get("/api/v1/reports" + path, params=params)
    assert result.status_code == 200
    payload = result.json()
    assert set(payload) == {"success", "data"} and payload["success"] is True
    return payload["data"]


@pytest.mark.parametrize(
    "method,path", [("get", "/999"), ("post", "/999/knowledge-card")]
)
def test_missing_report_has_unchanged_404_envelope(reader, method, path):
    result = getattr(reader.client, method)("/api/v1/reports" + path)
    assert result.status_code == 404
    assert result.json() == {"detail": "Report not found"}
    assert reader.commits == []


@pytest.mark.parametrize(
    "params,ids",
    [
        ({}, [1, 2, 3, 4]),
        ({"market": "us"}, [1, 3]),
        ({"asset_class": "equity"}, [1, 2]),
        ({"frequency": "daily"}, [1, 3]),
        ({"topic": "momentum"}, [2]),
        ({"search": "portfolio"}, [1, 4]),
        ({"search": "allocation"}, [1, 2]),
        ({"source": "local_pdf"}, [2]),
        ({"has_full_text": "true"}, [1, 3]),
        ({"has_full_text": "false"}, [2, 4]),
        ({"date_from": "2026-10-10"}, [1, 3]),
        ({"date_to": "2026-10-10T23:59:59"}, [1, 2]),
        ({"sort_by": "published_date"}, [3, 1, 2, 4]),
        ({"sort_by": "title"}, [2, 4, 1, 3]),
        ({"sort_by": "unknown"}, [4, 3, 2, 1]),
        (
            {
                "market": "us",
                "asset_class": "equity",
                "frequency": "daily",
                "topic": "portfolio",
                "source": "arxiv",
                "search": "allocation",
                "has_full_text": "true",
                "date_from": "2026-10-10",
                "date_to": "2026-10-11",
            },
            [1],
        ),
        ({"search": "missing"}, []),
    ],
)
def test_list_filters_sort_and_defaults(reader, params, ids):
    data = get_data(reader, **params)
    assert set(data) == {"reports", "total", "limit", "offset"}
    assert [r["id"] for r in data["reports"]] == ids
    assert (data["total"], data["limit"], data["offset"]) == (len(ids), 50, 0)
    assert reader.commits == []


@pytest.mark.parametrize("offset,ids", [(1, [4, 1]), (99, [])])
def test_list_pagination_keeps_total_before_slicing(reader, offset, ids):
    data = get_data(reader, sort_by="title", limit=2, offset=offset)
    assert [r["id"] for r in data["reports"]] == ids
    assert (data["total"], data["limit"], data["offset"]) == (4, 2, offset)


@pytest.mark.parametrize(
    "method,path,params,location",
    [
        ("get", "", {"limit": 0}, ["query", "limit"]),
        ("get", "", {"limit": 201}, ["query", "limit"]),
        ("get", "", {"limit": "bad"}, ["query", "limit"]),
        ("get", "", {"offset": -1}, ["query", "offset"]),
        ("get", "", {"has_full_text": "bad"}, ["query", "has_full_text"]),
        ("get", "/bad", {}, ["path", "report_id"]),
        ("post", "/1/knowledge-card", {"force": "bad"}, ["query", "force"]),
    ],
)
def test_validation_remains_at_the_http_boundary(
    reader, method, path, params, location
):
    result = getattr(reader.client, method)("/api/v1/reports" + path, params=params)
    assert result.status_code == 422
    assert set(result.json()) == {"detail"}
    assert result.json()["detail"][0]["loc"] == location
    assert reader.commits == []


def test_list_and_detail_keep_distinct_current_and_legacy_wire_fields(reader):
    listed = get_data(reader)["reports"][0]
    detail = get_data(reader, "/1")
    fields = BASE_FIELDS | (CURRENT_FIELDS if reader.current else set())
    assert set(listed) == fields
    assert set(detail) == fields | DETAIL_FIELDS | (
        {"knowledge_card"} if reader.current else set()
    )
    for key in fields:
        assert detail[key] == listed[key]
    assert listed["authors"] == ["First Author", "Second Author"]
    assert listed["published_date"] == "2026-10-10T00:00:00"
    assert listed["custom_tags"] == ["reading-list"]
    assert listed["markets"] == ["us"]
    assert listed["asset_classes"] == ["equity"]
    assert listed["frequencies"] == ["daily"]
    assert listed["topics"] == ["portfolio"]
    assert detail["summary"] == "Saved summary"
    assert detail["tables_json"] == '[{"caption": "Results"}]'
    assert detail["equations_json"] == '[{"latex": "x=y"}]'
    assert detail["analysis"] == {"summary": "Unsigned historical analysis"}
    with reader.factory() as db:
        assert detail["full_text"] == db.get(Report, 1).full_text
        assert db.get(Report, 1).knowledge_card_json is None
    if reader.current:
        assert listed["publication_date_info"] == {
            "precision": "day",
            "evidence": "10 October 2026",
        }
        assert listed["classification_provenance"] == {
            "method": "llm",
            "evidence": ["portfolio"],
        }
        assert listed["selection_relevance"] == 8
        assert listed["evaluation"]["status"] == "reviewed"
        assert detail["knowledge_card"]["status"] == "ready"
    assert reader.commits == []


@pytest.mark.parametrize(
    "analysis",
    [None, "broken", "[]", '{"analysis_fingerprint": "stale", "depth": "standard"}'],
)
def test_detail_hides_unreadable_or_stale_analysis_without_erasing_it(reader, analysis):
    with reader.factory() as db:
        db.get(Report, 1).analysis_json = analysis
        db.commit()
    reader.commits.clear()
    assert "analysis" not in get_data(reader, "/1")
    with reader.factory() as db:
        assert db.get(Report, 1).analysis_json == analysis
    assert reader.commits == []


def test_stats_reuses_repository_implementation_and_preserves_envelope(
    reader, monkeypatch
):
    original = ReportRepository.get_stats
    calls = []

    def observed(repo):
        calls.append(repo)
        return original(repo)

    monkeypatch.setattr(ReportRepository, "get_stats", observed)
    assert get_data(reader, "/stats") == {
        "total_reports": 4,
        "with_full_text": 2,
        "without_full_text": 2,
    }
    assert len(calls) == 1
    assert reader.commits == []


def test_knowledge_card_refresh_commits_only_changed_projection_and_preserves_evidence(
    reader,
    monkeypatch,
):
    from reportagent.services import knowledge_cards

    original = knowledge_cards.get_knowledge_card
    forces = []

    def observed(report, *, force=False):
        forces.append(force)
        return original(report, force=force)

    monkeypatch.setattr(knowledge_cards, "get_knowledge_card", observed)
    with reader.factory() as db:
        row = db.get(Report, 1)
        evidence = (row.evaluation_json, row.analysis_json, row.summary, row.full_text)
        row.knowledge_card_json = '{"highlights": ["fabricated"]}'
        db.commit()
    reader.commits.clear()
    path = "/api/v1/reports/1/knowledge-card"
    first_response = reader.client.post(path)
    assert first_response.status_code == 200
    assert set(first_response.json()) == {"success", "data"}
    assert first_response.json()["success"] is True
    first = first_response.json()["data"]
    assert first["status"] == "ready" and first["quality_score"] is None
    assert first["evidence"] and "fabricated" not in str(first)
    assert len(reader.commits) == 1
    assert reader.client.post(path).json()["data"] == first
    assert reader.client.post(path, params={"force": "true"}).json()["data"] == first
    assert len(reader.commits) == 1
    with reader.factory() as db:
        row = db.get(Report, 1)
        assert (
            row.evaluation_json,
            row.analysis_json,
            row.summary,
            row.full_text,
        ) == evidence
        saved_card = row.knowledge_card_json
        assert json.loads(saved_card) == first
        row.full_text += " changed material"
        db.commit()
    reader.commits.clear()
    pending = reader.client.post(path, params={"force": "true"}).json()["data"]
    assert pending["status"] == "pending" and pending["evidence"] == []
    assert reader.commits == []
    with reader.factory() as db:
        row = db.get(Report, 1)
        assert row.knowledge_card_json == saved_card
        assert (row.evaluation_json, row.analysis_json, row.summary) == evidence[:3]
    assert forces == [False, False, True, True]


def test_adapter_execution_modes_and_serializer_imports_are_compatible():
    for name in ["list_reports", "get_stats", "get_report"]:
        assert not inspect.iscoroutinefunction(getattr(library, name))
    assert not inspect.iscoroutinefunction(library.get_knowledge_card)
    assert library._report_to_dict is report_views.report_to_dict


@pytest.mark.parametrize(
    "method,path,service_name",
    [
        ("get", "", "list_reports"),
        ("get", "/1", "get_report_detail"),
        ("post", "/1/knowledge-card", "get_knowledge_card"),
    ],
)
def test_reader_executes_the_shared_read_service(
    reader, monkeypatch, method, path, service_name
):
    # Fail before extraction, then wrap the real implementation rather than return
    # a canned response: output-only equivalence would miss duplicated workflows.
    service = getattr(reader.adapter, "report_reads", None)
    assert service is not None, "adapter must delegate to the shared read service"
    assert library.report_reads is service
    original = getattr(service, service_name)
    calls = []

    def observed(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(service, service_name, observed)
    result = getattr(reader.client, method)("/api/v1/reports" + path)
    assert result.status_code == 200
    assert result.json()["success"] is True
    assert len(calls) == 1
    if service_name == "list_reports":
        assert len(result.json()["data"]["reports"]) == 4
    elif service_name == "get_report_detail":
        assert result.json()["data"]["title"] == "Portfolio research"
    else:
        assert result.json()["data"]["status"] == "ready"


def test_detail_calls_the_single_analysis_freshness_policy(reader, monkeypatch):
    original = report_views.read_current_analysis
    calls = []

    def observed(report):
        calls.append(report.id)
        return original(report)

    monkeypatch.setattr(report_views, "read_current_analysis", observed)
    assert get_data(reader, "/1")["analysis"] == {
        "summary": "Unsigned historical analysis"
    }
    assert calls == [1]


@pytest.mark.parametrize("depth", ["quick", "standard", "deep"])
def test_detail_uses_current_signed_analysis_then_hides_it_after_material_change(
    reader, depth
):
    from reportagent.services.batch_analysis import analysis_cache_fingerprint

    with reader.factory() as db:
        row = db.get(Report, 1)
        analysis = {
            "depth": depth,
            "analysis_fingerprint": analysis_cache_fingerprint(row, depth),
            "summary": "Current signed analysis",
        }
        row.analysis_json = json.dumps(analysis)
        db.commit()
    reader.commits.clear()
    assert get_data(reader, "/1")["analysis"] == analysis
    assert reader.commits == []
    with reader.factory() as db:
        db.get(Report, 1).full_text += "Material revised after analysis"
        db.commit()
    reader.commits.clear()
    assert "analysis" not in get_data(reader, "/1")
    assert reader.commits == []
    with reader.factory() as db:
        assert json.loads(db.get(Report, 1).analysis_json) == analysis


def test_nullable_summary_fields_keep_empty_arrays_and_null_dates(reader):
    row = get_data(reader, search="Beta")["reports"][0]
    for key in ("authors", "custom_tags"):
        assert row[key] == []
    for key in (
        "abstract",
        "published_date",
        "source_url",
        "doi",
        "arxiv_id",
        "pdf_path",
    ):
        assert row[key] is None
    if reader.current:
        assert row["publication_date_info"] == {}
        assert row["classification_provenance"] == {}
        assert row["evaluation"] is None
        assert row["selection_relevance"] is None
