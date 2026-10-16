import asyncio
from unittest.mock import AsyncMock

from types import SimpleNamespace
import pytest


def idea(**extra):
    return {'hypothesis': 'Conditional risk may improve allocation', 'gap_status': 'candidate',
            'closest_paper_ids': [1], 'difference': 'Test conditional rather than constant risk',
            'data_requirements': 'Historical observations with timestamps',
            'minimal_experiment': 'Compare to a constant risk baseline on a held-out period',
            'falsification': 'No improvement in held-out loss', **extra}


def context():
    return {'retrieval_status': 'available', 'papers': [{'id': 1, 'title': 'Risk paper',
            'abstract': 'Original abstract TAIL_EVIDENCE', 'source_url': 'https://example.org/1',
            'evidence_scope': 'abstract'}], 'external_status': 'not_requested', 'external_leads': []}


def test_model_receives_full_available_material_and_scope(monkeypatch):
    from reportagent.services import research_ideation as r
    monkeypatch.setattr(r, 'collect_context', lambda *a, **k: context())
    model = SimpleNamespace(chat_json=AsyncMock(return_value={'ideas': [idea()]}))
    result = asyncio.run(r.generate_ideas('risk', llm=model))
    assert 'TAIL_EVIDENCE' in str(model.chat_json.call_args)
    assert '候选' in result and 'https://example.org/1' in result
    assert '最小验证' in result and '否定' in result


@pytest.mark.parametrize('bad', [idea(closest_paper_ids=[999]), idea(gap_status='confirmed')])
def test_unknown_citation_or_confirmed_novelty_is_not_published(monkeypatch, bad):
    from reportagent.services import research_ideation as r
    monkeypatch.setattr(r, 'collect_context', lambda *a, **k: context())
    model = SimpleNamespace(chat_json=AsyncMock(return_value={'ideas': [bad]}))
    result = asyncio.run(r.generate_ideas('risk', llm=model))
    assert '待重试' in result and 'Conditional risk may improve' not in result


def test_retrieval_error_does_not_masquerade_as_empty_search(monkeypatch):
    from reportagent.services import research_ideation as r
    monkeypatch.setattr(r, 'collect_context', lambda *a, **k: context() | {'retrieval_status': 'error'})
    model = SimpleNamespace(chat_json=AsyncMock())
    result = asyncio.run(r.generate_ideas('risk', llm=model))
    assert '检索失败' in result
    model.chat_json.assert_not_called()


def test_empty_library_labels_unsourced_hypotheses(monkeypatch):
    from reportagent.services import research_ideation as r
    monkeypatch.setattr(r, 'collect_context', lambda *a, **k: context() | {'retrieval_status': 'empty', 'papers': []})
    model = SimpleNamespace(chat_json=AsyncMock(return_value={'ideas': [idea(closest_paper_ids=[])]}))
    result = asyncio.run(r.generate_ideas('risk', llm=model))
    assert '未找到' in result and '不代表' in result and '未查新' in result


def test_model_initialization_failure_preserves_scope(monkeypatch):
    from reportagent.services import research_ideation as r
    import reportagent.llm.client as models
    monkeypatch.setattr(r, 'collect_context', lambda *a, **k: context())
    def broken():
        raise ValueError('missing configuration')
    monkeypatch.setattr(models, 'LLMClient', broken)
    result = asyncio.run(r.generate_ideas('risk'))
    assert '待重试' in result and '有限检索' in result
