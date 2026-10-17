"""Offline contract tests for abstract-only relevance screening."""

import asyncio
import json
import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.models.database import Base, Report
from reportagent.services import paper_scoring as scoring


@pytest.fixture
def direction_config(tmp_path, monkeypatch):
    path = tmp_path / "configs" / "push_directions.yaml"
    path.parent.mkdir()
    data = {
        "directions": [
            {
                "id": "new_direction",
                "name": "New direction",
                "enabled": True,
                "description": "New research scope",
                "keywords": ["tail risk"],
            },
            {
                "id": "disabled_direction",
                "name": "Disabled direction",
                "enabled": False,
            },
        ],
        "push_settings": {"llm_filter_threshold": 6.0},
    }
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setattr(scoring, "PROJECT_ROOT", tmp_path, raising=False)
    return path, data


class OfflineModel:
    provider = "offline"
    model = "fixture"

    def __init__(self, response):
        self.response = response
        self.prompts = []

    async def chat_json(self, messages, **kwargs):
        self.prompts.append(messages)
        return self.response


def score(response, papers=None):
    papers = papers or [{"title": "Paper"}]
    papers = [
        {
            "abstract": "We study portfolio tail risk using conditional forecasting and compare risk estimates.",
            **paper,
        }
        for paper in papers
    ]
    return asyncio.run(scoring.score_papers_batch(OfflineModel(response), papers))


def test_enabled_config_and_complete_abstract_reach_model(direction_config):
    abstract = "Background. " * 100 + "ESSENTIAL RESULT AT END"
    model = OfflineModel([{"index": 0, "score": 8, "reason": "Relevant"}])
    results, failures = asyncio.run(
        scoring.score_papers_batch(model, [{"title": "Paper", "abstract": abstract}])
    )
    assert not failures
    system, user = model.prompts[0]
    assert "New research scope" in system["content"]
    assert "tail risk" in system["content"]
    assert "disabled_direction" not in system["content"] + user["content"]
    assert abstract in user["content"]
    assert results[0]["best_direction"] is None


def test_relevance_is_primary_not_composite_quality_score(direction_config):
    results, failures = score(
        [
            {
                "index": 0,
                "score": 4,
                "relevance": 9,
                "innovation": 1,
                "practicality": 1,
                "best_direction": "new_direction",
                "reason": "Core topic",
            }
        ]
    )
    assert not failures
    assert results[0]["score"] == results[0]["relevance"] == 9


@pytest.mark.parametrize(
    "score_value", [-1, 10.1, float("nan"), float("inf"), None, True, "8"]
)
def test_invalid_score_is_retryable(direction_config, score_value):
    results, failures = score([{"index": 0, "score": score_value, "reason": "Invalid"}])
    assert results == []
    assert len(failures) == 1 and failures[0][:2] == (0, 1)


@pytest.mark.parametrize("index", [-1, 1, 0.0, True, "0", None])
def test_invalid_index_never_attaches_a_paper(direction_config, index):
    results, failures = score([{"index": index, "score": 8, "reason": "Invalid"}])
    assert results == []
    assert failures


@pytest.mark.parametrize("response", [{"other": []}, {"papers": {}}, [None], []])
def test_malformed_or_empty_response_is_reported_as_retryable(
    direction_config, response
):
    results, failures = score(response)
    assert results == []
    assert failures and failures[0][:2] == (0, 1)


@pytest.mark.parametrize("direction", ["disabled_direction", "unknown", [], 12])
def test_non_enabled_direction_is_retryable(direction_config, direction):
    results, failures = score(
        [{"index": 0, "score": 8, "best_direction": direction, "reason": "Invalid"}]
    )
    assert results == []
    assert failures


def test_duplicate_and_missing_indices_stay_unjudged_while_valid_sibling_survives(
    direction_config,
):
    papers = [{"title": title} for title in ["duplicate", "valid", "missing"]]
    results, failures = score(
        [
            {"index": 0, "score": 8, "reason": "First"},
            {"index": 0, "score": 9, "reason": "Conflicting"},
            {"index": 1, "score": 7, "reason": "Valid"},
        ],
        papers,
    )
    assert [item["_paper"]["title"] for item in results] == ["valid"]
    assert failures[0][:2] == (0, 2)


def test_invalid_present_relevance_cannot_fall_back_to_valid_score(direction_config):
    results, failures = score(
        [{"index": 0, "score": 8, "relevance": None, "reason": "Invalid"}]
    )
    assert results == [] and failures


def test_scoring_fingerprint_tracks_direction_and_prompt_changes(
    direction_config, monkeypatch, tmp_path
):
    path, data = direction_config
    initial = scoring.scoring_fingerprint()
    assert initial == scoring.scoring_fingerprint()
    data["directions"][0]["description"] = "Changed research scope"
    path.write_text(yaml.safe_dump(data))
    changed = scoring.scoring_fingerprint()
    assert changed != initial
    original_resource = scoring.skill_resource
    prompt = tmp_path / "scoring-system.md"
    prompt.write_text(
        original_resource("paper-selection", "scoring-system.md").read_text()
        + "\nChanged rubric."
    )
    monkeypatch.setattr(
        scoring,
        "skill_resource",
        lambda skill, name: prompt
        if name == "scoring-system.md"
        else original_resource(skill, name),
    )
    assert scoring.scoring_fingerprint() != changed


@pytest.fixture
def memory_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(scoring, "init_db", lambda: None)
    monkeypatch.setattr(scoring, "get_session_factory", lambda: factory)
    with factory() as session:
        session.add(
            Report(
                title="Paper",
                source="arxiv",
                content_hash="fixture",
                abstract="We study portfolio tail risk using conditional forecasting and compare risk estimates.",
            )
        )
        session.commit()
    yield factory
    engine.dispose()


def test_apply_scores_persists_relevance_and_ignores_advisory_rejection(
    direction_config, memory_db
):
    results, _ = score(
        [
            {
                "index": 0,
                "score": 2,
                "relevance": 8,
                "best_direction": "new_direction",
                "is_selected": False,
                "reason": "Core topic",
            }
        ],
        [{"id": 1, "title": "Paper"}],
    )
    stats = scoring.apply_scores(results)
    assert stats["selected"] == 1 and stats["errors"] == 0
    with memory_db() as session:
        paper = session.get(Report, 1)
        assert paper.selection_score == paper.selection_relevance == 8
        assert paper.selection_direction == "new_direction"
        assert paper.is_selected is True
        assert paper.selection_reason == "Core topic"
        provenance = json.loads(paper.selection_provenance_json)
        assert provenance["evidence_scope"] == "title_abstract"
        assert provenance["fingerprint"] == scoring.scoring_fingerprint()


def test_configured_threshold_is_used_when_not_overridden(direction_config, memory_db):
    path, data = direction_config
    data["push_settings"]["llm_filter_threshold"] = 8.5
    path.write_text(yaml.safe_dump(data))
    results, _ = score(
        [{"index": 0, "score": 8, "is_selected": True, "reason": "Related"}],
        [{"id": 1, "title": "Paper"}],
    )
    assert scoring.apply_scores(results)["rejected"] == 1
    assert scoring.apply_scores(results, threshold=8)["selected"] == 1


def test_apply_scores_cannot_round_a_below_threshold_score_into_selection(
    direction_config, memory_db
):
    results, _ = score(
        [{"index": 0, "score": 5.99, "is_selected": True, "reason": "Related"}],
        [{"id": 1, "title": "Paper"}],
    )
    assert scoring.apply_scores(results, threshold=6)["rejected"] == 1


def test_dry_run_does_not_open_or_initialize_database(direction_config, monkeypatch):
    def fail():
        pytest.fail("dry run must not initialize/open a database")

    monkeypatch.setattr(scoring, "init_db", fail)
    monkeypatch.setattr(scoring, "get_session_factory", fail)
    results, _ = score(
        [{"index": 0, "score": 8, "reason": "Related"}], [{"id": 1, "title": "Paper"}]
    )
    assert scoring.apply_scores(results, dry_run=True)["selected"] == 1


def test_apply_revalidates_untrusted_direct_results(direction_config, memory_db):
    stats = scoring.apply_scores(
        [
            {
                "_paper": {"id": 1},
                "score": float("nan"),
                "is_selected": True,
                "reason": "Invalid",
            }
        ]
    )
    assert stats["errors"] == 1 and stats["selected"] == 0
    with memory_db() as session:
        assert session.get(Report, 1).selection_reason is None


def test_changed_criteria_rescore_old_results_and_unchanged_criteria_skip_model(
    direction_config, memory_db, monkeypatch
):
    model = OfflineModel([{"index": 0, "score": 8, "reason": "Related"}])
    monkeypatch.setattr(scoring, "LLMClient", lambda: model)
    options = scoring.ScoringOptions(all_unjudged=True)
    asyncio.run(scoring.score_reports(options))
    with memory_db() as session:
        assert session.get(Report, 1).is_selected is True
    path, data = direction_config
    data["directions"][0]["description"] = "Changed scope"
    path.write_text(yaml.safe_dump(data))
    model.response = [{"index": 0, "score": 2, "reason": "No longer related"}]
    asyncio.run(scoring.score_reports(options))
    with memory_db() as session:
        assert session.get(Report, 1).is_selected is False
        assert session.get(Report, 1).selection_relevance == 2
    monkeypatch.setattr(
        scoring,
        "LLMClient",
        lambda: pytest.fail("Current scores must not call the model again"),
    )
    asyncio.run(scoring.score_reports(options))


def test_failed_persistence_is_visible_and_keeps_paper_retryable(
    direction_config, memory_db, monkeypatch
):
    monkeypatch.setattr(
        scoring,
        "LLMClient",
        lambda: OfflineModel([{"index": 0, "score": 8, "reason": "Related"}]),
    )
    monkeypatch.setattr(
        scoring.ReportRepository, "update_selection", lambda *args, **kwargs: False
    )
    with pytest.raises(RuntimeError, match="persist"):
        asyncio.run(scoring.score_reports(scoring.ScoringOptions(all_unjudged=True)))
    with memory_db() as session:
        assert session.get(Report, 1).selection_reason is None
