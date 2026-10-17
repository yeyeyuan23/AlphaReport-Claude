"""Reusable analysis must still render links for the current library and host."""

import asyncio
import hashlib
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.models.database import Base
from reportagent.models.schemas import SearchResult, SourceType
from reportagent.services import one_report
from reportagent.services.report_quality import review_fingerprint
from tests.support.report_fixtures import TEXT, complete_analysis


@pytest.mark.parametrize(
    "old_id,old_host,include_model",
    [
        (999, "https://old.example", True),
        (1, "https://old.example", True),
        (999, "https://current.example", False),
    ],
)
def test_cached_brief_refreshes_current_links_and_markdown_without_model(
    tmp_path, monkeypatch, old_id, old_host, include_model
):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fixture")
    paper = SearchResult(
        title="Current portfolio paper",
        authors=["Author"],
        source=SourceType.ARXIV,
        source_url="https://arxiv.org/abs/2601.12345v1",
        arxiv_id="2601.12345v1",
        abstract="We investigate portfolio allocation with trading costs.",
        full_text=TEXT,
        abstract_only=False,
        pdf_path=str(pdf),
    )
    monkeypatch.setattr(one_report, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(one_report, "init_db", lambda: None)
    monkeypatch.setattr(one_report, "get_session_factory", lambda: factory)
    monkeypatch.setattr(one_report, "collect_paper", AsyncMock(return_value=paper))
    monkeypatch.setattr(one_report, "analysis_fingerprint", lambda: "current-policy")
    monkeypatch.setattr(one_report, "get_llm_model", lambda: "fixture-model")
    monkeypatch.setattr(
        one_report, "get_web_base_url", lambda: "https://current.example/"
    )

    def no_model(*_, **__):
        raise AssertionError("Valid cached analysis must not invoke a model")

    monkeypatch.setattr(one_report, "LLMClient", no_model)
    try:
        report_id = one_report.store_paper(paper)
        old_link = f"{old_host}/library/{old_id}"
        receipts = [
            {
                "chat_id": "oc_saved",
                "message_id": "saved",
                "sent_at": "2026-01-01T00:00:00Z",
            }
        ]
        cache = {
            "report_id": old_id,
            "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
            "analysis_fingerprint": "current-policy",
            "analysis": complete_analysis(),
            "evaluation": {
                "status": "reviewed",
                "tier": "browse",
                "fingerprint": review_fingerprint(
                    one_report._stored_review_input(report_id), "fixture-model"
                ),
            },
            "brief": f"Old brief: {old_link}",
            "deliveries": receipts,
        }
        if include_model:
            cache["model"] = "fixture-model"
        path = tmp_path / "data" / "one_reports" / f"{paper.arxiv_id}.json"
        one_report.save_artifact(path, cache)
        path.with_suffix(".md").write_text(cache["brief"])
        returned_path, result = asyncio.run(one_report.prepare_report(paper.arxiv_id))
        current_link = f"https://current.example/library/{report_id}"
        assert returned_path == path
        assert result["report_id"] == report_id
        assert current_link in result["brief"]
        assert old_link not in result["brief"]
        assert paper.source_url in result["brief"]
        assert paper.title in result["brief"]
        assert result["deliveries"] == receipts
        assert result["analysis"] == cache["analysis"]
        assert result["evaluation"] == cache["evaluation"]
        assert json.loads(path.read_text()) == result
        assert path.with_suffix(".md").read_text() == result["brief"]
        assert asyncio.run(one_report.prepare_report(paper.arxiv_id))[1] == result
    finally:
        engine.dispose()
