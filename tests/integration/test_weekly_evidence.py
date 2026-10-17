"""Weekly delivery reuses current saved evidence; relevance is never a quality label."""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.models.database import Base, Report
from reportagent.services import weekly_report as weekly
from reportagent.services.paper_scoring import scoring_fingerprint, evidence_fingerprint
from reportagent.services.report_quality import review_fingerprint, SCHEMA_VERSION
from reportagent.utils.config import get_llm_model
from tests.support.report_fixtures import TEXT, response


def paper(rid=1, **changes):
    result = {
        "id": rid,
        "title": "带条件的组合优化",
        "source": "arxiv",
        "source_url": f"https://arxiv.org/abs/2610.0000{rid}",
        "arxiv_id": f"2610.0000{rid}v1",
        "abstract": "This paper analyzes entropy regularization and convexity for constrained dynamic portfolios.",
        "full_text": TEXT,
        "has_full_text": True,
        "selection_relevance": 8,
        "selection_score": 8,
        "selection_reason": "与组合优化有关",
        "selection_direction": "portfolio_optimization",
        "published_date": "2026-10-09",
        "ui_url": f"http://localhost/library/{rid}",
    }
    result.update(changes)
    result["selection_provenance_json"] = json.dumps(
        {
            "fingerprint": scoring_fingerprint(),
            "status": "scored",
            "evidence_fingerprint": evidence_fingerprint(result),
        }
    )
    return result


def assessed(p, **changes):
    evidence = {
        "claim": "作者证明：只有温度充分小时才保持凸性，过大时可能失去凸性。",
        "quote": "We use a rolling out-of-sample evaluation with transaction costs.",
        "chunk_id": "C001",
        "verified": True,
        "locator": "提取文本段 C001",
    }
    e = {
        **response(),
        "method": evidence,
        "findings": [evidence],
        "strengths": [evidence],
        "status": "reviewed",
        "tier": "focus",
        "label": "重点阅读",
        "validation_issues": [],
        "text_complete": True,
        "quality": "strong",
        "confidence": "high",
        "relevance": 8,
        "schema_version": SCHEMA_VERSION,
        "model": get_llm_model(),
        "fingerprint": review_fingerprint(p, get_llm_model()),
        **changes,
    }
    p["evaluation"] = e
    p["evaluation_json"] = json.dumps(e, ensure_ascii=False)
    return p


@pytest.fixture
def library(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(weekly, "init_db", lambda: None)
    monkeypatch.setattr(weekly, "get_session_factory", lambda: factory)
    yield factory
    engine.dispose()


def test_week_query_covers_more_than_100_and_retains_full_abstract(library):
    with library() as session:
        session.add_all(
            [
                Report(
                    title=f"Paper-{i}",
                    source="arxiv",
                    content_hash=str(i),
                    abstract="完整摘要" * 300,
                    published_date=datetime(2026, 10, 9),
                )
                for i in range(207)
            ]
        )
        session.commit()
    rows = weekly.get_weekly_papers(datetime(2026, 10, 5), datetime(2026, 10, 12))
    assert len(rows) == 207
    assert len(rows[0]["abstract"]) == 1200


def test_weekly_never_promotes_relevance_or_legacy_summary_to_quality():
    p = paper(summary="正则化必然提升样本外收益，应当立即调仓。")
    result = weekly.generate_weekly_report([p], "2026-10-05", "2026-10-11")
    assert "重点推荐 0 篇" in result
    assert "待核验" in result
    assert p["summary"] not in result
    assert "⭐" not in result


def test_weekly_uses_verified_qualifiers_without_new_model_generation():
    p = assessed(paper())
    result = weekly.generate_weekly_report([p], "2026-10-05", "2026-10-11")
    assert "量化研究周报" in result
    assert "只有温度充分小时才保持凸性" in result
    assert "重点推荐 1 篇" in result
    assert p["source_url"] in result


def test_stale_evidence_cannot_be_featured_after_text_changes():
    p = assessed(paper())
    p["full_text"] += "New source evidence."
    result = weekly.generate_weekly_report([p], "2026-10-05", "2026-10-11")
    assert "重点推荐 0 篇" in result
    assert "只有温度充分小时才保持凸性" not in result


def test_pending_weekly_explains_question_without_publishing_unverified_findings():
    p = assessed(
        paper(),
        tier="pending",
        status="needs_verification",
        research_question="禁止卖空时，熵正则化在什么条件下保持凸性？",
        findings=[{"claim": "可以保证赚取超额收益"}],
        validation_issues=["引文未能定位：C005", "引文未能定位：C014"],
    )
    result = weekly.generate_weekly_report([p], "2026-10-05", "2026-10-11")
    assert "禁止卖空时，熵正则化在什么条件下保持凸性" in result
    assert "待核验" in result
    assert "可以保证赚取超额收益" not in result
    assert "C005" not in result


def test_no_llm_preview_does_not_call_models_or_send(library, tmp_path, monkeypatch):
    with library() as session:
        session.add(
            Report(
                title="待阅读",
                source="arxiv",
                content_hash="one",
                published_date=datetime(2026, 10, 9),
            )
        )
        session.commit()
    monkeypatch.setattr(
        weekly,
        "get_previous_week_range",
        lambda *args: (
            datetime(2026, 10, 5, tzinfo=timezone.utc),
            datetime(2026, 10, 11, 23, 59, 59, tzinfo=timezone.utc),
            "2026-10-05",
            "2026-10-11",
        ),
    )
    send = MagicMock(side_effect=AssertionError("No sending in preview"))
    monkeypatch.setattr(weekly, "send_feishu_card", send)
    result = weekly.run_weekly_report(
        weekly.WeeklyReportOptions(
            dry_run=True, no_llm=True, output=str(tmp_path / "week.md")
        )
    )
    assert result["status"] == "preview"
    assert result["model_refresh"] is False
    assert "待核验" in (tmp_path / "week.md").read_text()
    send.assert_not_called()


def test_native_weekly_prepares_shared_evidence_instead_of_claude(
    library, tmp_path, monkeypatch
):
    with library() as session:
        session.add(
            Report(
                title="待阅读",
                source="arxiv",
                content_hash="one",
                published_date=datetime(2026, 10, 9),
            )
        )
        session.commit()
    monkeypatch.setattr(
        weekly,
        "get_previous_week_range",
        lambda *args: (
            datetime(2026, 10, 5),
            datetime(2026, 10, 11, 23, 59, 59),
            "2026-10-05",
            "2026-10-11",
        ),
    )
    prepare = AsyncMock(return_value={"screened": 0, "review_ids": [], "failures": []})
    monkeypatch.setattr(weekly, "prepare_weekly_evidence", prepare, raising=False)
    result = weekly.run_weekly_report(
        weekly.WeeklyReportOptions(dry_run=True, output=str(tmp_path / "week.md"))
    )
    prepare.assert_awaited_once()
    assert result["model_refresh"] is True
    assert result["sent"] == 0


def test_weekly_review_budget_advances_past_current_cached_papers(library, monkeypatch):
    from reportagent.services import report_quality

    with library() as session:
        for rid in range(1, 14):
            values = paper(
                rid=rid,
                title=f"Portfolio study {rid}",
                published_date=datetime(2026, 10, 9),
            )
            stored = {
                k: v for k, v in values.items() if k in Report.__table__.columns.keys()
            }
            row = Report(**stored, content_hash=str(rid))
            if rid < 13:
                review = assessed(values)["evaluation"]
                row.evaluation_json = json.dumps(review)
            session.add(row)
        session.commit()
    review = AsyncMock(return_value={13: {"tier": "browse", "status": "reviewed"}})
    monkeypatch.setattr(report_quality, "review_report_ids", review)
    papers = weekly.get_weekly_papers(datetime(2026, 10, 5), datetime(2026, 10, 12))
    result = asyncio.run(weekly.prepare_weekly_evidence(papers, max_reviews=12))
    assert result["review_ids"] == [13]
    review.assert_awaited_once_with([13], force=False, max_reviews=12, audit={})
