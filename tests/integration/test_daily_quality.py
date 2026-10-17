import asyncio
import json
from datetime import timedelta
from unittest.mock import patch

import pytest

from reportagent.services.report_periods import get_daily_range
from reportagent.models.database import Report
from reportagent.services import daily_push as daily
from tests.support.report_fixtures import Model, paper, response, module


def evaluated_paper(i, category="arxiv"):
    p = paper(
        id=i,
        source=category,
        title=f"Paper {i}",
        source_url=f"https://example.org/{i}",
        ui_url=f"http://localhost/library/{i}",
    )
    p["evaluation"] = asyncio.run(module().evaluate_paper(p, Model(response())))
    return p


def test_brief_prioritizes_five_and_explains_others_without_changing_findings():
    from reportagent.services.report_briefing import render_briefing

    papers = [evaluated_paper(i) for i in range(7)] + [evaluated_paper(9, "eastmoney")]
    brief = render_briefing(
        papers,
        "fixture",
        source_statuses=[
            {
                "source": "arxiv",
                "status": "partial",
                "valid": 7,
                "errors": ["PDF timeout"],
            }
        ],
    )
    assert brief.count("**研究问题**") == 5
    assert "滚动样本外检验" in brief
    assert "跨市场适用性未报告" in brief
    assert "券商研报" in brief
    assert "部分成功" in brief
    assert "http://localhost/library/9" in brief


def test_pending_papers_never_fill_empty_focus_section():
    from reportagent.services.report_briefing import render_briefing

    p = paper(id=1, title="Pending", ui_url="http://localhost/library/1")
    p["evaluation"] = module().pending_review(p, "全文获取失败")
    brief = render_briefing([p], "fixture")
    assert "暂无达到重点推荐标准的论文" in brief
    assert "全文获取失败" in brief


def test_zero_current_source_results_can_feature_existing_library_paper_with_clear_counts():
    from reportagent.services.report_briefing import render_briefing

    p = evaluated_paper(1)
    brief = render_briefing(
        [p],
        "fixture",
        source_statuses=[
            {"source": "arxiv", "status": "error", "valid": 0, "errors": ["HTTP 429"]},
            {"source": "eastmoney", "status": "empty", "valid": 0},
            {"source": "bigquant", "status": "empty", "valid": 0},
        ],
    )
    assert "本次在线采集有效资料 0 条" in brief
    assert "本期检查 1 篇库中候选" in brief
    assert "重点推荐 1 篇" in brief
    assert "采集有效条数可能包含已有条目的更新" in brief
    assert "覆盖可能不完整" in brief


def test_valid_source_count_is_not_treated_as_selected_paper_count():
    from reportagent.services.report_briefing import render_briefing

    brief = render_briefing(
        [evaluated_paper(1)],
        "fixture",
        source_statuses=[
            {"source": "arxiv", "status": "ok", "valid": 3},
        ],
    )
    assert "本次在线采集有效资料 3 条" in brief
    assert "本期检查 1 篇库中候选" in brief
    assert "重点推荐 1 篇" in brief


@pytest.mark.parametrize(
    ("source", "label"),
    [("arxiv", "学术论文"), ("eastmoney", "券商研报"), ("bigquant", "社区研究")],
)
def test_reading_priority_features_sound_work_across_sources_and_discloses_limits(
    source, label
):
    from reportagent.services.report_briefing import render_briefing

    data = response()
    data.update(
        quality="sound",
        confidence="medium",
        strengths=[],
        limitations=["原文明示：仅覆盖一个市场。"],
    )
    p = paper(id=1, source=source, title="值得优先读的研究", selection_relevance=8)
    p["evaluation"] = asyncio.run(module().evaluate_paper(p, Model(data)))
    brief = render_briefing([p], "fixture")
    assert "重点推荐 1 篇" in brief
    assert "**研究问题**" in brief
    assert "仅覆盖一个市场" in brief
    assert label in brief


def test_daily_does_not_select_old_score_after_abstract_changes(memory_session_factory):
    from reportagent.services.paper_scoring import (
        evidence_fingerprint,
        scoring_fingerprint,
    )

    factory = memory_session_factory
    with factory() as session:
        row = Report(
            title="Old score",
            source="arxiv",
            content_hash="stale",
            abstract="We evaluate dynamic portfolios and transaction costs.",
            published_date=get_daily_range()[0] + timedelta(hours=1),
            announced_at=get_daily_range()[0] + timedelta(hours=1),
            selection_relevance=9,
            is_selected=True,
        )
        row.selection_provenance_json = json.dumps(
            {
                "fingerprint": scoring_fingerprint(),
                "status": "scored",
                "evidence_fingerprint": evidence_fingerprint(vars(row)),
            }
        )
        row.abstract = "Updated abstract studies a different problem with new evidence."
        session.add(row)
        session.commit()
    with (
        patch.object(daily, "init_db"),
        patch.object(daily, "get_session_factory", return_value=factory),
    ):
        result = daily.get_todays_papers()
    assert result[0] == []


def test_report_date_and_original_publication_date_are_distinct():
    from reportagent.services.report_briefing import render_briefing

    p = evaluated_paper(1)
    p["published_date"] = "2026-10-15T00:00:00"
    text = render_briefing([p], "2026-10-17")
    assert "2026-10-17" in text
    assert "原始提交日期（UTC）：2026-10-15" in text


def test_daily_template_edits_take_effect_without_another_model_call(tmp_path):
    from reportagent.services import report_briefing

    template = tmp_path / "briefing.md"
    template.write_text("{date_label}\n{other_papers}")
    p = paper(id=1, title="Fixture paper", ui_url="http://localhost/library/1")
    p["evaluation"] = module().pending_review(p, "全文获取失败")
    with patch.object(report_briefing, "skill_resource", return_value=template):
        assert "Fixture paper" in report_briefing.render_briefing([p], "2026-10-16")
        template.write_text("UPDATED {date_label}\n{other_papers}")
        assert report_briefing.render_briefing([p], "2026-10-16").startswith(
            "UPDATED 2026-10-16"
        )


def test_daily_reads_saved_reviews_before_render_and_dry_run_never_delivers(
    tmp_path, memory_session_factory
):
    factory = memory_session_factory
    with factory() as s:
        s.add(
            Report(
                id=1,
                title="Paper",
                source="arxiv",
                content_hash="one",
                is_selected=True,
                published_date=get_daily_range()[0] + timedelta(hours=1),
                announced_at=get_daily_range()[0] + timedelta(hours=1),
            )
        )
        s.commit()
    events = []
    # Do not nest asyncio.run in the fake model boundary.
    evaluation = evaluated_paper(1)["evaluation"]

    def review(ids, **kwargs):
        events.append("read")
        with factory() as session:
            row = session.get(Report, 1)
            evaluation["fingerprint"] = module().review_fingerprint(
                vars(row), evaluation["model"]
            )
            row.evaluation_json = json.dumps(evaluation)
            session.commit()
        return {1: evaluation}

    def render(papers, *args, **kwargs):
        events.append("render")
        assert papers[0]["evaluation"]["tier"] == "focus"
        return "Preview grounded in evidence"

    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/push_directions.yaml").write_text("push_settings: {}")
    with (
        patch.object(daily, "PROJECT_ROOT", tmp_path),
        patch.object(daily, "init_db"),
        patch.object(daily, "get_session_factory", return_value=factory),
        patch.object(daily, "sync_public_url"),
        patch.object(daily, "read_saved_reviews", side_effect=review),
        patch.object(daily, "render_briefing", side_effect=render),
        patch.object(daily, "send_feishu_card") as send,
    ):
        result = daily.run_daily_push(
            daily.DailyPushOptions(report_ids="1", dry_run=True)
        )
    assert events == ["read", "render"]
    assert result["status"] == "preview"
    send.assert_not_called()


def test_duplicate_daily_job_is_rejected_before_work_starts(tmp_path):
    with patch.object(daily, "PROJECT_ROOT", tmp_path):
        with daily.daily_run_lock():
            with pytest.raises(RuntimeError, match="正在"):
                with daily.daily_run_lock():
                    pytest.fail("Second job entered")
