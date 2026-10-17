import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reportagent.filters.direction_filter import DirectionFilter


ABSTRACT = "We study portfolio allocation using conditional risk forecasts and evaluate out of sample."


@pytest.fixture(autouse=True)
def risk_rubric(monkeypatch):
    from reportagent.services import paper_scoring

    monkeypatch.setattr(
        paper_scoring,
        "_load_scoring_config",
        lambda: {
            "directions": [{"id": "risk", "name": "Risk"}],
        },
    )


def setup_filter():
    f = DirectionFilter()
    f._directions = [{"id": "risk", "name": "Risk"}]
    return f


def papers():
    return [{"id": 1, "title": "Risk study", "abstract": ABSTRACT}]


def item(score=7):
    return {
        "index": 0,
        "scores": [
            {"direction_id": "risk", "score": score, "reason": "Risk forecasting"}
        ],
    }


def assess_response(response, filt=None):
    """Exercise the canonical model-response parser through its live adapter."""
    filt = filt or setup_filter()
    filt._llm_client = SimpleNamespace(chat_json=AsyncMock(return_value=response))
    return asyncio.run(filt.score_papers(papers()))[0]


@pytest.mark.parametrize("score", [15, -1, float("inf"), float("nan"), True, "7"])
def test_invalid_score_is_retryable_not_selected(score):
    r = assess_response([item(score)])
    assert r.status == "retryable_error"
    assert r.max_score is None
    assert not r.is_selected


def test_threshold_is_used_by_result_and_selection():
    f = setup_filter()
    f.SELECTION_THRESHOLD = 8
    r = assess_response([item(7)], f)
    assert not r.is_selected
    assert f.get_selected_papers([r]) == []


@pytest.mark.parametrize(
    "payload", [[], [item(), item()], [None], [item() | {"scores": []}]]
)
def test_missing_duplicate_or_invalid_response_remains_retryable(payload):
    r = assess_response(payload)
    assert r.status == "retryable_error"
    assert not r.is_selected


def test_complete_abstract_is_sent():
    f = setup_filter()
    assert "TAIL_EVIDENCE" in f.build_scoring_prompt(
        [papers()[0] | {"abstract": ABSTRACT * 20 + "TAIL_EVIDENCE"}]
    )


def test_missing_material_does_not_call_model():
    f = setup_filter()
    f._llm_client = SimpleNamespace(chat_json=AsyncMock())
    r = asyncio.run(f.score_papers([papers()[0] | {"abstract": ""}]))[0]
    assert r.status == "pending_needs_material" and r.max_score is None
    f._llm_client.chat_json.assert_not_called()


def test_model_failure_is_retryable():
    f = setup_filter()
    f._llm_client = SimpleNamespace(
        chat_json=AsyncMock(side_effect=ValueError("incomplete output"))
    )
    r = asyncio.run(f.score_papers(papers()))[0]
    assert r.status == "retryable_error" and not r.is_selected


def test_explicit_error_status_cannot_become_selected():
    r = assess_response([item() | {"status": "retryable_error"}])
    assert r.status == "retryable_error" and not r.is_selected


def test_model_initialization_failure_is_retryable(monkeypatch):
    import reportagent.llm.client as models

    def broken():
        raise ValueError("missing configuration")

    monkeypatch.setattr(models, "LLMClient", broken)
    assert (
        asyncio.run(setup_filter().score_papers(papers()))[0].status
        == "retryable_error"
    )


def test_valid_canonical_scalar_result_is_preserved_by_detail_adapter():
    r = assess_response(
        [{"index": 0, "score": 7, "best_direction": "risk", "reason": "Risk evidence"}]
    )
    assert r.status == "scored" and r.max_score == 7
    assert r.best_direction == "risk" and r.is_selected
    assert r.assessment["reason"] == "Risk evidence"
