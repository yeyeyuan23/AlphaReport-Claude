"""Weekly delivery reuses current saved evidence; relevance is never a quality label."""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from reportagent.models.database import Report
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
def library(monkeypatch, memory_session_factory):
    factory = memory_session_factory
    monkeypatch.setattr(weekly, "init_db", lambda: None)
    monkeypatch.setattr(weekly, "get_session_factory", lambda: factory)
    from reportagent.services import collection_reviews

    monkeypatch.setattr(collection_reviews, "get_session_factory", lambda: factory)
    yield factory


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


@pytest.mark.parametrize("source", ["eastmoney", "bigquant"])
def test_week_query_preserves_current_review_for_papers_without_arxiv_id(
    library, source
):
    p = assessed(
        paper(source=source, arxiv_id=None, published_date=datetime(2026, 10, 9))
    )
    with library() as session:
        values = {k: v for k, v in p.items() if k in Report.__table__.columns.keys()}
        session.add(Report(**values, content_hash="current-non-arxiv"))
        session.commit()

    rows = weekly.get_weekly_papers(datetime(2026, 10, 5), datetime(2026, 10, 12))
    text = weekly.generate_weekly_report(rows, "2026-10-05", "2026-10-11")
    assert "重点推荐 1 篇" in text
    assert "只有温度充分小时才保持凸性" in text
    assert "## 待核验" not in text


def test_week_query_keeps_saved_material_failure_instead_of_generic_stale_notice(
    library,
):
    from reportagent.services.report_quality import pending_review

    p = paper(
        published_date=datetime(2026, 10, 9),
        abstract=None,
        full_text=None,
        has_full_text=False,
    )
    reason = "PDF 下载失败，需要重试取得全文。"
    p["evaluation_json"] = json.dumps(pending_review(p, reason), ensure_ascii=False)
    with library() as session:
        values = {k: v for k, v in p.items() if k in Report.__table__.columns.keys()}
        session.add(Report(**values, content_hash="missing-material"))
        session.commit()

    rows = weekly.get_weekly_papers(datetime(2026, 10, 5), datetime(2026, 10, 12))
    text = weekly.generate_weekly_report(rows, "2026-10-05", "2026-10-11")
    assert reason in text
    assert "尚无与当前原文" not in text


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
    assert p["ui_url"] in result


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
        "get_weekly_range",
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
        weekly.WeeklyReportOptions(dry_run=True, output=str(tmp_path / "week.md"))
    )
    assert result["status"] == "preview"
    assert "待核验" in (tmp_path / "week.md").read_text()
    send.assert_not_called()


def test_collection_review_does_not_rescore_stale_relevance(library, monkeypatch):
    from reportagent.services import paper_scoring, report_quality

    with library() as session:
        values = paper(
            rid=1,
            published_date=datetime(2026, 10, 9),
        )
        stored = {
            k: v for k, v in values.items() if k in Report.__table__.columns.keys()
        }
        row = Report(**stored, content_hash="stale")
        assert row.published_date is not None
        assert row.selection_relevance == 8
        assert paper_scoring.screening_is_current(row)
        row.selection_provenance_json = None
        session.add(row)
        session.commit()

    async def forbidden(*args, **kwargs):
        pytest.fail("Collection must not rescore relevance or review a stale decision")

    monkeypatch.setattr(paper_scoring, "score_papers_batch", forbidden)
    monkeypatch.setattr(report_quality, "review_report_ids", forbidden)
    from reportagent.services import collection_reviews

    result = asyncio.run(collection_reviews.prepare_collection_reviews())
    assert result["unreviewed_candidates"] == 0
    assert result["review_ids"] == []


def test_weekly_direction_updates_include_every_paper_without_recommendation_lists(
    monkeypatch,
):
    from reportagent.services import weekly_briefing

    monkeypatch.setattr(
        weekly_briefing,
        "load_enabled_directions",
        lambda: {
            "portfolio_optimization": {"name": "组合优化"},
            "ml_quant": {"name": "机器学习"},
        },
    )
    first_claim = "作者证明：只有温度充分小时才保持凸性，过大时可能失去凸性。"
    second_claim = "作者发现交易成本改变了最优换手率。"
    third_claim = "其他组合发现仍须保留。"
    papers = [
        assessed(
            paper(
                rid=i,
                title=f"研究{i}",
                published_date="2026-10-15",
            ),
            findings=[
                {
                    "claim": first_claim
                    if i <= 2
                    else second_claim
                    if i == 3
                    else third_claim
                }
            ],
        )
        for i in range(1, 8)
    ]
    papers.append(
        assessed(
            paper(
                rid=8,
                title="较早公告",
                selection_direction="ml_quant",
                published_date="2026-10-15",
            ),
            findings=[{"claim": "模型在分布漂移下需要重新校准。"}],
        )
    )
    papers.extend(
        [
            assessed(
                paper(rid=9, title="待核验论文"),
                tier="pending",
                status="needs_verification",
                findings=[{"claim": "未经核验的发现不得进入方向概要。"}],
            ),
            assessed(
                paper(rid=10, title="排除论文"),
                tier="exclude",
                findings=[{"claim": "已排除的发现不得进入方向概要。"}],
            ),
        ]
    )
    text = weekly.generate_weekly_report(papers, "2026-10-10", "2026-10-17")
    updates = text.split("## 本周研究更新", 1)[1].split("\n## ", 1)[0]
    portfolio, machine_learning = updates.split("**机器学习 · 1 篇**")
    assert "**组合优化 · 7 篇**" in portfolio
    assert portfolio.count(first_claim) == 2
    assert portfolio.count(second_claim) == 1
    assert portfolio.count(third_claim) == 4
    for p in papers[:7]:
        assert f"[{p['title']}]({p['ui_url']})" in portfolio
        assert p["ui_url"] not in machine_learning
    assert "模型在分布漂移下需要重新校准。" not in portfolio
    assert "模型在分布漂移下需要重新校准。" in machine_learning
    assert f"[较早公告]({papers[7]['ui_url']})" in machine_learning
    assert first_claim not in machine_learning
    assert "依据：" not in updates
    assert "未经核验的发现" not in updates and "已排除的发现" not in updates
    assert papers[8]["ui_url"] not in updates and papers[9]["ui_url"] not in updates
    assert "重点推荐 8 篇" in text
    assert "## 本周重点推荐" not in text
    assert "## 其他可浏览论文" not in text
    assert all(text.count(f"http://localhost/library/{i})") == 1 for i in range(1, 9))
    assert "**方法与发现**" not in text and "**阅读建议**" not in text
    assert first_claim in text
