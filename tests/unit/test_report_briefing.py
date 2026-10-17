from unittest.mock import patch

import pytest

from reportagent.models.schemas import (
    AnalysisResult,
    Assessment,
    Methodology,
    SearchResult,
    SourceType,
)
from reportagent.services import one_report, paper_scoring, report_briefing


def paper_and_analysis():
    paper = SearchResult(
        title="Fixture paper",
        authors=["Fixture author"],
        source=SourceType.ARXIV,
        source_url="https://arxiv.org/abs/2005.13665v1",
        arxiv_id="2005.13665v1",
        raw_metadata={"pdf_url": "https://arxiv.org/pdf/2005.13665v1"},
    )
    analysis = AnalysisResult(
        research_question="旧研究问题",
        core_contribution="旧核心贡献",
        summary="旧总结",
        methodology=Methodology(analysis_points=[{"analysis": "旧方法"}]),
        assessment=Assessment(strengths=["旧优势"], weaknesses=["旧限制"]),
    )
    return paper, analysis


def evaluation(**changes):
    evidence = {
        "claim": "已保存的研究发现",
        "quote": "A source excerpt that can be located.",
        "chunk_id": "C001",
        "verified": True,
        "locator": "提取文本段 C001",
    }
    return {
        "tier": "browse",
        "label": "值得浏览",
        "status": "reviewed",
        "source_category": "academic",
        "research_question": "已保存的研究问题",
        "method": {**evidence, "claim": "已保存的方法"},
        "findings": [evidence],
        "strengths": [{**evidence, "claim": "已保存的优点"}],
        "limitations": ["已保存的样本限制"],
        "critical_concerns": [],
        "validation_issues": [],
        "reading_advice": "已保存的阅读建议",
        "relevance": None,
        "relevance_reason": "方向匹配",
        "rejected_evidence": [{"claim": "审计中被拒绝的主张"}],
        **changes,
    }


def test_single_brief_prefers_saved_evaluation_and_includes_source_and_detail_links():
    paper, analysis = paper_and_analysis()
    with patch.dict(
        "os.environ", {"ALPHAREPORT_BASE_URL": "https://research.example/"}
    ):
        brief = one_report.make_brief(
            paper, analysis, "fixture-model", evaluation(), report_id=42
        )
    for content in [
        "值得浏览",
        "已保存的研究问题",
        "已保存的方法",
        "已保存的研究发现",
        "已保存的样本限制",
        "已保存的阅读建议",
        "https://arxiv.org/abs/2005.13665v1",
        "https://research.example/library/42",
    ]:
        assert content in brief
    assert "旧研究问题" not in brief
    assert "旧总结" not in brief
    assert "审计中被拒绝的主张" not in brief


def test_single_pending_brief_discloses_validation_issue_without_presenting_findings():
    paper, analysis = paper_and_analysis()
    pending = evaluation(
        tier="pending",
        label="待核验",
        status="needs_verification",
        validation_issues=["引文未能定位：C001"],
    )
    brief = one_report.make_brief(paper, analysis, "fixture-model", pending)
    assert "待核验" in brief
    assert "旧版引文匹配筛查已停用" in brief
    assert pending["validation_issues"] == ["引文未能定位：C001"]
    assert "已保存的研究发现" not in brief
    assert "已保存的方法" not in brief


def test_historical_single_brief_preserves_legacy_analysis_without_evaluation():
    paper, analysis = paper_and_analysis()
    brief = one_report.make_brief(paper, analysis, "fixture-model")
    for content in [
        "旧研究问题",
        "旧核心贡献",
        "旧方法",
        "旧总结",
        "旧限制",
        paper.source_url,
    ]:
        assert content in brief


def test_single_template_edits_change_output_and_analysis_fingerprint(tmp_path):
    template = tmp_path / "single-brief.md"
    template.write_text("FIRST {title}\n{research_question}", encoding="utf-8")
    original = one_report.skill_resource

    def resource(skill, name):
        return template if name == "single-brief.md" else original(skill, name)

    paper, analysis = paper_and_analysis()
    with (
        patch.object(one_report, "skill_resource", side_effect=resource),
        patch.object(report_briefing, "skill_resource", side_effect=resource),
    ):
        before = one_report.analysis_fingerprint()
        first = one_report.make_brief(paper, analysis, "fixture-model")
        template.write_text("SECOND {title}\n{research_question}", encoding="utf-8")
        after = one_report.analysis_fingerprint()
        second = one_report.make_brief(paper, analysis, "fixture-model")
    assert before != after
    assert first.startswith("FIRST Fixture paper")
    assert second.startswith("SECOND Fixture paper")


def test_daily_quicklist_displays_configured_direction_name():
    paper = {
        "id": 1,
        "title": "Fixture paper",
        "source": "arxiv",
        "evaluation": evaluation(),
        "selection_direction": "a_share_factor",
    }
    config = {
        "directions": [{"id": "a_share_factor", "name": "A股测试方向", "enabled": True}]
    }
    with patch.object(paper_scoring, "_load_scoring_config", return_value=config):
        brief = report_briefing.render_briefing([paper], "fixture")
    assert "A股测试方向" in brief
    assert "a_share_factor" not in brief


def test_completed_review_does_not_repeat_legacy_screening_status_as_its_recommendation_reason():
    saved = evaluation(
        relevance_reason="[portfolio_optimization] [仅摘要相关性；全文质量待评审] 与组合优化有关。"
    )
    sections = report_briefing.evaluation_sections(saved)
    assert "与组合优化有关" in sections["why_read"]
    assert "已保存的优点" in sections["why_read"]
    assert "portfolio_optimization" not in sections["why_read"]
    assert "全文质量待评审" not in sections["why_read"]
    assert saved["relevance_reason"].startswith("[portfolio_optimization]")


def test_research_conditions_in_brackets_are_preserved_in_recommendation_reason():
    saved = evaluation(
        relevance_reason="[条件 A] 参数需要处于给定范围，才可应用该结论。"
    )
    assert (
        "[条件 A] 参数需要处于给定范围"
        in report_briefing.evaluation_sections(saved)["why_read"]
    )


def test_daily_pending_quicklist_uses_validation_issues_instead_of_findings():
    paper = {
        "id": 1,
        "title": "Pending paper",
        "source": "arxiv",
        "evaluation": evaluation(
            tier="pending",
            label="待核验",
            status="needs_verification",
            validation_issues=["全文超过本次输入上限"],
        ),
    }
    brief = report_briefing.render_briefing([paper], "fixture")
    assert "全文超过本次输入上限" in brief
    assert "已保存的研究发现" not in brief


@pytest.mark.parametrize(
    "tier,label", [("focus", "重点阅读"), ("exclude", "不推荐"), ("browse", "值得浏览")]
)
def test_daily_unverified_status_is_pending_regardless_of_saved_tier(tier, label):
    paper = {
        "id": 1,
        "title": "Inconsistent saved review",
        "source": "arxiv",
        "evaluation": evaluation(
            tier=tier,
            label=label,
            status="needs_verification",
            validation_issues=["引文未能定位：C001"],
        ),
    }
    brief = report_briefing.render_briefing([paper], "fixture")
    assert "重点推荐 0 篇" in brief
    assert "旧版引文匹配筛查已停用" in brief
    assert paper["evaluation"]["validation_issues"] == ["引文未能定位：C001"]
    assert "已保存的研究发现" not in brief


def test_daily_briefing_includes_every_paper_detail_link():
    papers = [
        {
            "id": i,
            "title": f"Paper {i}",
            "ui_url": f"https://fixture.test/library/{i}",
        }
        for i in range(1, 21)
    ]
    brief = report_briefing.render_briefing(papers, "fixture")

    assert brief.count("[网页详情]") == 20
    for paper in papers:
        assert f"[网页详情]({paper['ui_url']})" in brief
