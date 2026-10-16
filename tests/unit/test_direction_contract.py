import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reportagent.filters.direction_filter import DirectionFilter


ABSTRACT = 'We study portfolio allocation using conditional risk forecasts and evaluate out of sample.'


def setup_filter():
    f = DirectionFilter()
    f._directions = [{'id': 'risk', 'name': 'Risk'}]
    return f


def papers():
    return [{'id': 1, 'title': 'Risk study', 'abstract': ABSTRACT}]


def item(score=7):
    return {'report_id': 1, 'scores': [{'direction_id': 'risk', 'score': score, 'reason': 'Risk forecasting'}]}


@pytest.mark.parametrize('score', [15, -1, float('inf'), float('nan'), True, '7'])
def test_invalid_score_is_retryable_not_selected(score):
    r = setup_filter()._parse_response([item(score)], papers())[0]
    assert r.status == 'retryable_error'
    assert r.max_score is None
    assert not r.is_selected


def test_threshold_is_used_by_result_and_selection():
    f = setup_filter()
    f.SELECTION_THRESHOLD = 8
    r = f._parse_response([item(7)], papers())[0]
    assert not r.is_selected
    assert f.get_selected_papers([r]) == []


@pytest.mark.parametrize('payload', [[], [item(), item()], [None], [item() | {'scores': []}]])
def test_missing_duplicate_or_invalid_response_remains_retryable(payload):
    r = setup_filter()._parse_response(payload, papers())[0]
    assert r.status == 'retryable_error'
    assert not r.is_selected


def test_complete_abstract_is_sent():
    f = setup_filter()
    assert 'TAIL_EVIDENCE' in f.build_scoring_prompt([papers()[0] | {'abstract': ABSTRACT * 20 + 'TAIL_EVIDENCE'}])


def test_missing_material_does_not_call_model():
    f = setup_filter()
    f._llm_client = SimpleNamespace(chat_json=AsyncMock())
    r = asyncio.run(f.score_papers([papers()[0] | {'abstract': ''}]))[0]
    assert r.status == 'pending_needs_material' and r.max_score is None
    f._llm_client.chat_json.assert_not_called()


def test_model_failure_is_retryable():
    f = setup_filter()
    f._llm_client = SimpleNamespace(chat_json=AsyncMock(side_effect=ValueError('incomplete output')))
    r = asyncio.run(f.score_papers(papers()))[0]
    assert r.status == 'retryable_error' and not r.is_selected


def test_explicit_error_status_cannot_become_selected():
    r = setup_filter()._parse_response([item() | {'status': 'retryable_error'}], papers())[0]
    assert r.status == 'retryable_error' and not r.is_selected


def test_model_initialization_failure_is_retryable(monkeypatch):
    import reportagent.llm.client as models
    def broken():
        raise ValueError('missing configuration')
    monkeypatch.setattr(models, 'LLMClient', broken)
    assert asyncio.run(setup_filter().score_papers(papers()))[0].status == 'retryable_error'
