"""A stored review is displayable only for the active material and policy."""
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, object_session

from reportagent.models.database import Base, Report
from reportagent.services import report_quality as quality
from reportagent.services import knowledge_cards
from reportagent.services.report_views import report_to_dict
from reportagent.utils import config
from tests.support.report_fixtures import paper, response


@pytest.fixture
def reviewed(monkeypatch):
    monkeypatch.setattr(config, "get_llm_model", lambda: "fixture-model")
    monkeypatch.setattr(quality, "get_llm_model", lambda: "fixture-model")
    monkeypatch.setattr(knowledge_cards, "get_llm_model", lambda: "fixture-model")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        data = paper()
        row = Report(**data, content_hash="fixture", authors="[]")
        session.add(row)
        session.flush()
        evaluation = {**response(), "schema_version": quality.SCHEMA_VERSION,
            "status": "reviewed", "tier": "focus", "label": "重点阅读", "relevance": 8,
            "text_complete": True, "model": "fixture-model",
            "fingerprint": quality.review_fingerprint(data, "fixture-model")}
        row.evaluation_json = json.dumps(evaluation)
        session.commit()
        yield row, evaluation
    engine.dispose()


def test_current_complete_review_is_still_visible(reviewed):
    row, evaluation = reviewed
    shown = report_to_dict(row)["evaluation"]
    assert shown["status"] == "reviewed" and shown["tier"] == "focus"
    assert shown["fingerprint"] == evaluation["fingerprint"]


@pytest.mark.parametrize("change", ["relevance", "material", "abstract", "model", "rubric", "missing_fingerprint", "no_full_text", "schema"])
def test_stale_review_never_displays_old_score_or_recommendation(reviewed, monkeypatch, change):
    row, evaluation = reviewed
    original_cache = row.evaluation_json
    if change == "relevance":
        row.selection_relevance = row.selection_score = 2
        row.selection_reason = "New screening: unrelated"
    elif change == "material":
        row.full_text += " Revised methods."
    elif change == "abstract":
        row.abstract = "Updated abstract."
    elif change == "model":
        monkeypatch.setattr(config, "get_llm_model", lambda: "other-model")
    elif change == "rubric":
        monkeypatch.setattr(quality, "scoring_fingerprint", lambda: "changed-rubric")
    elif change == "missing_fingerprint":
        evaluation.pop("fingerprint")
        row.evaluation_json = original_cache = json.dumps(evaluation)
    elif change == "schema":
        evaluation["schema_version"] = quality.SCHEMA_VERSION - 1
        row.evaluation_json = original_cache = json.dumps(evaluation)
    else:
        row.has_full_text = False
    shown = report_to_dict(row)["evaluation"]
    assert shown["status"] == "pending" and shown["tier"] == "pending"
    assert shown["quality"] == "unknown" and shown["findings"] == []
    assert shown["validation_issues"]
    assert row.evaluation_json == original_cache  # rendering never erases historical evidence
    if change == "relevance":
        assert shown["relevance"] == 2


def test_library_detail_and_list_share_the_freshness_gate(reviewed):
    from reportagent.api import library
    row, _ = reviewed
    row.selection_relevance = row.selection_score = 2
    session = object_session(row)
    detail = library.get_report(row.id, db=session)
    assert detail["data"]["evaluation"]["tier"] == "pending"
    listed = library.list_reports(market=None, asset_class=None, frequency=None, topic=None,
        search=None, source=None, has_full_text=None, date_from=None, date_to=None,
        sort_by="created_at", limit=50, offset=0, db=session)
    assert listed["data"]["reports"][0]["evaluation"]["tier"] == "pending"


@pytest.mark.parametrize("reason", ["全文300000字符超过240000，需提高限额。", "全文评估未完成（TimeoutError），可重试。"])
def test_current_pending_preserves_actionable_reason_and_original_time(reviewed, reason):
    from reportagent.services.knowledge_cards import report_snapshot
    row, _ = reviewed
    saved = quality.pending_review(report_snapshot(row), reason)
    saved["reviewed_at"] = "2026-10-16T09:00:00+00:00"
    row.evaluation_json = json.dumps(saved)
    for _ in range(2):
        shown = report_to_dict(row)["evaluation"]
        assert shown["validation_issues"] == [reason]
        assert shown["reviewed_at"] == saved["reviewed_at"]
        assert shown["quality"] == "unknown" and shown["findings"] == []
    row.full_text += " Changed material."
    stale = report_to_dict(row)["evaluation"]
    assert stale["validation_issues"] != [reason]
    assert stale["reviewed_at"] == saved["reviewed_at"]
