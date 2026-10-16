import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.unit.test_knowledge_card_evidence import reviewed_paper


def output():
    quote = 'We use a rolling out-of-sample evaluation with transaction costs.'
    fields = {name: {'status': 'documented', 'value': '作者使用滚动样本外检验并计入成本',
                     'evidence': [{'quote': quote, 'chunk_id': 'C001'}]}
              for name in ('target', 'available_information', 'training_objective', 'data', 'split', 'baseline', 'metrics', 'assumptions')}
    return {'fields': fields, 'minimum_check': '建议：保留末段测试期，比较简单基准并检验成本敏感性。',
            'falsification': '建议：样本外损失未改善则不支持假设。'}


def test_preparation_preserves_evidence_and_separates_proposal():
    from reportagent.services.reproduction import prepare_reproduction
    model = SimpleNamespace(chat_json=AsyncMock(return_value=output()))
    card = asyncio.run(prepare_reproduction(reviewed_paper(), llm=model))
    assert card['status'] == 'ready'
    assert card['fields']['target']['evidence'][0]['raw_quote']
    assert card['proposal_label'] == '建议验证方案，非作者实验或已完成复现'
    assert '尚未复现' in card['notice']


def test_fabricated_fact_does_not_produce_successful_card():
    from reportagent.services.reproduction import prepare_reproduction
    data = output();data['fields']['target']['evidence'][0]['quote'] = 'Invented text not found anywhere in the original paper.'
    model = SimpleNamespace(chat_json=AsyncMock(return_value=data))
    card = asyncio.run(prepare_reproduction(reviewed_paper(), llm=model))
    assert card['status'] == 'retryable_error'
    assert not card.get('fields')


def test_preparation_needs_current_full_text_evidence():
    from reportagent.services.reproduction import prepare_reproduction
    model = SimpleNamespace(chat_json=AsyncMock())
    card = asyncio.run(prepare_reproduction({'title': 'Only title'}, llm=model))
    assert card['status'] == 'pending'
    model.chat_json.assert_not_called()


def test_cache_reuse_and_source_change():
    from reportagent.services.reproduction import prepare_reproduction
    p = reviewed_paper()
    model = SimpleNamespace(chat_json=AsyncMock(return_value=output()))
    card = asyncio.run(prepare_reproduction(p, llm=model))
    p['analysis_json'] = json.dumps({'reproduction_preparation': card})
    assert asyncio.run(prepare_reproduction(p, llm=model)) == card
    assert model.chat_json.await_count == 1
    p['full_text'] += 'revision changed'
    assert asyncio.run(prepare_reproduction(p, llm=model))['status'] == 'pending'


def test_missing_material_is_not_filled_with_invented_facts():
    from reportagent.services.reproduction import prepare_reproduction
    data = output();data['fields']['training_objective'] = {'status': 'needs_verification', 'value': '需核对优化目标', 'evidence': []}
    card = asyncio.run(prepare_reproduction(reviewed_paper(), llm=SimpleNamespace(chat_json=AsyncMock(return_value=data))))
    assert card['status'] == 'ready'
    assert 'training_objective' in card['open_questions']


def test_model_initialization_failure_is_retryable(monkeypatch):
    from reportagent.services.reproduction import prepare_reproduction
    import reportagent.llm.client as models
    def broken():
        raise ValueError('missing configuration')
    monkeypatch.setattr(models, 'LLMClient', broken)
    assert asyncio.run(prepare_reproduction(reviewed_paper()))['status'] == 'retryable_error'
