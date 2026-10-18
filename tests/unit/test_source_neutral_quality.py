"""Changing the distribution channel must not change the evidence sent or priority."""

import asyncio
import json

import pytest

from reportagent.services import paper_scoring as scoring, report_quality as quality
from tests.support.report_fixtures import Model, paper, response


def test_screening_input_is_identical_across_distribution_channels():
    inputs = [
        scoring._format_papers_for_llm([paper(source=source)])
        for source in ("arxiv", "eastmoney", "bigquant", "local_pdf")
    ]
    assert len(set(inputs)) == 1


def test_review_input_and_decision_are_identical_across_channels():
    models = [Model(response()) for _ in range(4)]
    results = [
        asyncio.run(quality.evaluate_paper(paper(source=source), model))
        for source, model in zip(
            ("arxiv", "eastmoney", "bigquant", "local_pdf"), models
        )
    ]
    assert len({json.dumps(model.requests, sort_keys=True) for model in models}) == 1
    assert {result["tier"] for result in results} == {"focus"}


def test_review_budget_uses_relevance_not_document_channel():
    rows = [
        paper(id=1, source="arxiv", selection_relevance=6),
        paper(id=2, source="bigquant", selection_relevance=9),
        paper(id=3, source="eastmoney", selection_relevance=8),
    ]
    assert [p["id"] for p in quality.select_candidates(rows, 2)] == [2, 3]


def test_no_limitations_does_not_force_an_invented_weakness():
    data = response()
    data["limitations"] = []
    assert asyncio.run(quality.evaluate_paper(paper(), Model(data)))["tier"] == "focus"


@pytest.mark.parametrize("research_type", ["broker", "community"])
def test_research_design_cannot_be_a_distribution_channel(research_type):
    data = response()
    data["research_type"] = research_type
    with pytest.raises(ValueError):
        asyncio.run(quality.evaluate_paper(paper(), Model(data)))


def test_current_quality_requires_an_explicit_dimension_assessment():
    data = response()
    data.pop("quality_checks", None)
    with pytest.raises(ValueError):
        asyncio.run(quality.evaluate_paper(paper(), Model(data)))


def test_screening_prompt_uses_the_same_object_contract_as_json_provider():
    from reportagent.llm.client import LLMClient

    directions, system, template, _ = scoring._scoring_context()
    messages = LLMClient._inject_json_instruction(
        [
            {"role": "system", "content": system.format(directions_desc="directions")},
            {
                "role": "user",
                "content": template.format(
                    papers_text="evidence", direction_ids=", ".join(directions)
                ),
            },
        ]
    )
    assert '"papers"' in messages[-1]["content"]
    assert "只返回 JSON 数组" not in messages[-1]["content"]


def test_analysis_and_review_load_the_same_quality_rubric():
    from reportagent.agents.analysis_agent import AnalysisAgent
    from reportagent.skills.registry import skill_resource

    rubric = skill_resource("analyze-report", "quality-rubric.md").read_text()
    for config in ("analysis.yaml", "one-report.yaml"):
        agent = AnalysisAgent(config_path=str(skill_resource("analyze-report", config)))
        assert rubric in agent._phase_config("assessment")["system_prompt"]
    model = Model(response())
    asyncio.run(quality.evaluate_paper(paper(), model))
    assert rubric in model.requests[0][0]["content"]


@pytest.mark.parametrize("dimension", ["method", "validation", "scope"])
def test_unclear_core_evidence_cannot_be_strong_or_high_confidence(dimension):
    from reportagent.models.schemas import QualityJudgment

    data = response()
    next(c for c in data["quality_checks"] if c["dimension"] == dimension)["status"] = (
        "unclear"
    )
    with pytest.raises(ValueError):
        QualityJudgment.model_validate(data)
    data.update(quality="sound", confidence="medium")
    assert QualityJudgment.model_validate(data).confidence == "medium"


def test_unknown_boolean_fields_are_not_silently_false():
    from reportagent.models.schemas import Reproducibility, AShareApplicability

    assert Reproducibility().has_data_source is None
    assert Reproducibility().has_code_available is None
    assert AShareApplicability().directly_applicable is None


def test_general_analysis_cache_invalidates_when_shared_rubric_changes(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace
    from reportagent.services import batch_analysis

    original = batch_analysis.skill_resource
    rubric = tmp_path / "quality-rubric.md"
    rubric.write_text("old")
    monkeypatch.setattr(
        batch_analysis,
        "skill_resource",
        lambda skill, name: rubric
        if name == "quality-rubric.md"
        else original(skill, name),
    )
    report = SimpleNamespace(**paper())
    before = batch_analysis.analysis_cache_fingerprint(report, "standard")
    rubric.write_text("new")
    assert batch_analysis.analysis_cache_fingerprint(report, "standard") != before


def test_single_paper_and_library_snapshots_keep_document_type(monkeypatch):
    from reportagent.models.database import Report
    from reportagent.services import one_report, knowledge_cards
    from tests.support.database import memory_database

    with memory_database() as factory:
        with factory() as session:
            row = Report(
                title="论文",
                source="arxiv",
                material_type="academic",
                content_hash="one",
            )
            session.add(row)
            session.commit()
            rid = row.id
            assert knowledge_cards.report_snapshot(row)["material_type"] == "academic"
        monkeypatch.setattr(one_report, "get_session_factory", lambda: factory)
        assert one_report._stored_review_input(rid)["material_type"] == "academic"


def test_material_label_refresh_does_not_change_saved_quality():
    from reportagent.services.report_views import read_evaluation

    candidate = paper(source="bigquant", material_type="community")
    model = Model(response())
    model.model = quality.get_llm_model()
    review = asyncio.run(quality.evaluate_paper(candidate, model))
    candidate["material_type"] = "broker"
    refreshed = read_evaluation(json.dumps(review), paper=candidate)
    assert refreshed["source_category"] == "broker"
    assert refreshed["quality"] == review["quality"]
    assert refreshed["tier"] == review["tier"]
