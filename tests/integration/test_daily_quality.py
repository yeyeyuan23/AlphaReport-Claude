import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.models.database import Base, Report, ReportDelivery
from reportagent.services import daily_push as daily
from tests.support.report_fixtures import Model, paper, response, module


def evaluated_paper(i, category="arxiv"):
    p = paper(id=i, source=category, title=f"Paper {i}", source_url=f"https://example.org/{i}", ui_url=f"http://localhost/library/{i}")
    p["evaluation"] = asyncio.run(module().evaluate_paper(p, Model(response())))
    return p


def test_brief_prioritizes_five_and_explains_others_without_changing_findings():
    from reportagent.services.report_briefing import render_briefing
    papers = [evaluated_paper(i) for i in range(7)] + [evaluated_paper(9, "eastmoney")]
    brief = render_briefing(papers, "fixture", source_statuses=[{"source": "arxiv", "status": "partial", "valid": 7, "errors": ["PDF timeout"]}])
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


def test_daily_template_edits_take_effect_without_another_model_call(tmp_path):
    from reportagent.services import report_briefing
    template = tmp_path / "briefing.md"
    template.write_text("{date_label}\n{other_papers}")
    p = paper(id=1, title="Fixture paper", ui_url="http://localhost/library/1")
    p["evaluation"] = module().pending_review(p, "全文获取失败")
    with patch.object(report_briefing, "skill_resource", return_value=template):
        assert "Fixture paper" in report_briefing.render_briefing([p], "2026-10-16")
        template.write_text("UPDATED {date_label}\n{other_papers}")
        assert report_briefing.render_briefing([p], "2026-10-16").startswith("UPDATED 2026-10-16")


def test_daily_reviews_before_render_and_dry_run_never_records_delivery(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        s.add(Report(id=1, title="Paper", source="arxiv", content_hash="one", is_selected=True,
                     published_date=datetime.now(timezone.utc)))
        s.commit()
    events = []
    # Do not nest asyncio.run in the fake model boundary.
    evaluation = evaluated_paper(1)["evaluation"]
    async def review(ids, **kwargs):
        events.append("review")
        with factory() as session:
            row = session.get(Report, 1)
            evaluation["fingerprint"] = module().review_fingerprint(vars(row), evaluation["model"])
            row.evaluation_json = json.dumps(evaluation)
            session.commit()
        return {1: evaluation}
    def render(papers, *args, **kwargs):
        events.append("render")
        assert papers[0]["evaluation"]["tier"] == "focus"
        return "Preview grounded in evidence"
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/push_directions.yaml").write_text("push_settings: {}")
    with patch.object(daily, "PROJECT_ROOT", tmp_path), patch.object(daily, "init_db"), patch.object(
        daily, "get_session_factory", return_value=factory
    ), patch.object(daily, "sync_public_url"), patch.object(daily, "review_report_ids", side_effect=review), patch.object(
        daily, "render_briefing", side_effect=render
    ), patch.object(daily, "send_feishu_card") as send:
        result = daily.run_daily_push(daily.DailyPushOptions(no_collect=True, report_ids="1", dry_run=True))
    assert events == ["review", "render"]
    assert result["status"] == "preview"
    send.assert_not_called()
    with factory() as s:
        assert s.query(ReportDelivery).count() == 0
    engine.dispose()


def test_all_source_failure_does_not_generate_a_successful_empty_digest(tmp_path):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/push_directions.yaml").write_text("push_settings: {}")
    with patch.object(daily, "PROJECT_ROOT", tmp_path), patch.object(daily, "collect_papers", return_value={
        "status": "failed", "source_statuses": [{"source": "arxiv", "status": "error"}]
    }), patch.object(daily, "sync_public_url"), patch.object(daily, "get_todays_papers") as query, patch.object(
        daily, "save_run_record"
    ):
        with pytest.raises(RuntimeError, match="来源"):
            daily.run_daily_push(daily.DailyPushOptions(dry_run=True))
    query.assert_not_called()


def test_duplicate_daily_job_is_rejected_before_work_starts(tmp_path):
    with patch.object(daily, "PROJECT_ROOT", tmp_path):
        with daily.daily_run_lock():
            with pytest.raises(RuntimeError, match="正在"):
                with daily.daily_run_lock():
                    pytest.fail("Second job entered")
