import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from reportagent.db.engine import get_db
from reportagent.models.database import Base, Report
from tests.unit.test_knowledge_card_evidence import reviewed_paper
from tests.unit.test_reproduction_preparation import output


@pytest.fixture
def library():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    p = reviewed_paper()
    with factory() as db:
        db.add(
            Report(
                **{k: v for k, v in p.items() if k in Report.__table__.columns.keys()},
                content_hash="evidence-card",
            )
        )
        db.commit()
    yield factory
    engine.dispose()


def client_for(factory, legacy=False):
    if legacy:
        from reportagent.api.legacy_app import app
    else:
        from reportagent.main import create_app

        app = create_app()

    def db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = db
    return TestClient(app)


@pytest.mark.parametrize("legacy", [False, True])
def test_web_cards_reject_legacy_cache_and_use_current_evidence(library, legacy):
    with library() as db:
        db.get(Report, 1).knowledge_card_json = json.dumps(
            {"highlights": ["fabricated 99%"], "quality_score": 9}
        )
        db.commit()
    client = client_for(library, legacy)
    try:
        first = client.post("/api/v1/reports/1/knowledge-card").json()["data"]
        assert first["status"] == "ready" and first["quality_score"] is None
        assert "fabricated" not in str(first)
        again = client.post("/api/v1/reports/1/knowledge-card?force=true").json()[
            "data"
        ]
        assert again == first
        with library() as db:
            report = db.get(Report, 1)
            report.full_text += "changed revision"
            db.commit()
        pending = client.post("/api/v1/reports/1/knowledge-card").json()["data"]
        assert pending["status"] == "pending" and not pending["highlights"]
        if not legacy:
            detail = client.get("/api/v1/reports/1").json()["data"]
            assert detail["knowledge_card"]["status"] == "pending"
        with library() as db:
            assert (
                json.loads(db.get(Report, 1).knowledge_card_json)["status"] == "ready"
            )
    finally:
        client.app.dependency_overrides.clear()
        client.close()


def test_reproduction_does_not_save_if_source_changes_during_generation(library):
    import asyncio
    from reportagent.services.reproduction import prepare_report

    async def change_source(*args, **kwargs):
        with library() as other:
            report = other.get(Report, 1)
            report.full_text += "concurrent change"
            other.commit()
        return output()

    model = SimpleNamespace(chat_json=AsyncMock(side_effect=change_source))
    with library() as db:
        result = asyncio.run(prepare_report(1, db, llm=model))
        assert result["status"] == "retryable_error"
        assert "reproduction_preparation" not in (db.get(Report, 1).analysis_json or "")


def test_reproduction_api_persists_valid_output_and_preserves_it_on_failure(
    library, monkeypatch
):
    import reportagent.llm.client as models

    llm = SimpleNamespace(chat_json=AsyncMock(return_value=output()))
    monkeypatch.setattr(models, "LLMClient", lambda: llm)
    client = client_for(library)
    try:
        assert (
            client.get("/api/v1/reports/1/reproduction-card").json()["data"]["status"]
            == "pending"
        )
        llm.chat_json.assert_not_called()
        result = client.post("/api/v1/reports/1/reproduction-card").json()["data"]
        assert result["status"] == "ready"
        assert (
            client.get("/api/v1/reports/1/reproduction-card").json()["data"] == result
        )
        assert (
            client.post("/api/v1/reports/1/reproduction-card").json()["data"] == result
        )
        assert llm.chat_json.await_count == 1
        llm.chat_json.side_effect = ValueError("truncated")
        assert (
            client.post("/api/v1/reports/1/reproduction-card?force=true").json()[
                "data"
            ]["status"]
            == "retryable_error"
        )
        assert (
            client.get("/api/v1/reports/1/reproduction-card").json()["data"] == result
        )
        with library() as db:
            report = db.get(Report, 1)
            report.full_text += "new revision"
            db.commit()
        assert (
            client.get("/api/v1/reports/1/reproduction-card").json()["data"]["status"]
            == "pending"
        )
        assert client.get("/api/v1/reports/999/reproduction-card").status_code == 404
    finally:
        client.app.dependency_overrides.clear()
        client.close()


def test_generate_button_api_works_with_full_text_and_no_saved_review(
    library, monkeypatch
):
    import reportagent.llm.client as models

    llm = SimpleNamespace(chat_json=AsyncMock(return_value=output()))
    monkeypatch.setattr(models, "LLMClient", lambda: llm)
    with library() as db:
        db.get(Report, 1).evaluation_json = None
        db.commit()
    client = client_for(library)
    try:
        result = client.post("/api/v1/reports/1/reproduction-card").json()["data"]
        assert result["status"] == "ready"
        assert (
            client.get("/api/v1/reports/1/reproduction-card").json()["data"]["status"]
            == "ready"
        )
        llm.chat_json.assert_awaited_once()
    finally:
        client.app.dependency_overrides.clear()
        client.close()
