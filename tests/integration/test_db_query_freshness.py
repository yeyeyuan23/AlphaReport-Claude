"""Bot-facing CLI details obey the same freshness policy as the HTTP reader."""

import json
import sys

import pytest

from reportagent.cli import db_query
from reportagent.models.database import Report
from reportagent.services.knowledge_cards import build_knowledge_card
from tests.support.report_fixtures import reviewed_paper


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("current_review", [False, True])
def test_cli_details_hide_stale_analysis_and_project_current_card(
    memory_session_factory, monkeypatch, capsys, full, current_review
):
    factory = memory_session_factory
    paper = reviewed_paper()
    if not current_review:
        paper["evaluation_json"] = None
    stale_analysis = json.dumps(
        {"analysis_fingerprint": "obsolete", "depth": "standard", "summary": "old"}
    )
    stale_card = json.dumps({"status": "ready", "summary": "obsolete findings"})
    with factory() as session:
        report = Report(
            **paper,
            content_hash="cli-freshness-fixture",
            analysis_json=stale_analysis,
            knowledge_card_json=stale_card,
            summary="Saved summary",
            publication_date_info_json='{"precision":"day"}',
        )
        session.add(report)
        session.commit()
        rid = report.id
    with factory() as session:
        expected_card = build_knowledge_card(session.get(Report, rid))
    monkeypatch.setattr(db_query, "init_db", lambda: None)
    monkeypatch.setattr(db_query, "get_session_factory", lambda: factory)
    monkeypatch.setattr(
        sys, "argv", ["db_query", "--get", str(rid)] + (["--full"] if full else [])
    )

    db_query.main()

    detail = json.loads(capsys.readouterr().out)
    assert "analysis" not in detail
    assert detail["knowledge_card"] == expected_card
    assert detail["knowledge_card"]["status"] == (
        "ready" if current_review else "pending"
    )
    assert detail["publication_date_info"] == {"precision": "day"}
    assert ("full_text" in detail) is full
    assert ("summary" in detail) is full
    if full:
        assert detail["full_text"] == paper["full_text"]
        assert detail["summary"] == "Saved summary"
    assert "tables_json" not in detail and "equations_json" not in detail
    with factory() as session:
        saved = session.get(Report, rid)
        assert saved.analysis_json == stale_analysis
        assert saved.knowledge_card_json == stale_card
