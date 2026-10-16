import asyncio
import json
from types import SimpleNamespace

from tests.support.report_fixtures import Model, module, paper, response


def reviewed_paper():
    p = paper()
    model = Model(response())
    model.model = module().get_llm_model()
    p['evaluation_json'] = json.dumps(asyncio.run(module().evaluate_paper(p, model)))
    return p


def test_card_reuses_located_evidence_without_new_quality_score():
    from reportagent.services.knowledge_cards import build_knowledge_card
    p = reviewed_paper()
    card = build_knowledge_card(p)
    evaluation = json.loads(p['evaluation_json'])
    assert card['status'] == 'ready'
    assert card['quality_score'] is None
    assert card['highlights'] == [x['claim'] for x in evaluation['findings']]
    assert all(e['verified'] and e['raw_quote'] in p['full_text'] for e in card['evidence'])


def test_legacy_analysis_or_card_cannot_supply_unverified_findings():
    from reportagent.services.knowledge_cards import build_knowledge_card
    card = build_knowledge_card(paper(analysis_json=json.dumps({'summary': 'invented 99%'})))
    assert card['status'] == 'pending'
    assert not card['highlights'] and not card['results']


def test_changed_text_or_invalid_quote_invalidates_saved_review():
    from reportagent.services.knowledge_cards import build_knowledge_card
    p = reviewed_paper()
    assert build_knowledge_card(p | {'full_text': p['full_text'] + 'changed'})['status'] == 'pending'
    e = json.loads(p['evaluation_json'])
    e['findings'][0]['quote'] = 'A sentence which does not exist in the actual paper.'
    assert build_knowledge_card(p | {'evaluation_json': json.dumps(e)})['status'] == 'pending'


def test_pending_cards_do_not_overwrite_saved_cache():
    from reportagent.services.knowledge_cards import get_knowledge_card
    r = SimpleNamespace(**paper(), evaluation_json=None, knowledge_card_json='previous saved card')
    card = get_knowledge_card(r, force=True)
    assert card['status'] == 'pending'
    assert r.knowledge_card_json == 'previous saved card'


def test_cache_identity_changes_with_prompt_and_model(tmp_path, monkeypatch):
    from reportagent.services import knowledge_cards as k
    p = reviewed_paper()
    before = k.build_knowledge_card(p)['provenance']['fingerprint']
    prompt = tmp_path / 'knowledge-card.md'
    prompt.write_text('New presentation rules')
    monkeypatch.setattr(k, 'skill_resource', lambda *args: prompt)
    assert k.build_knowledge_card(p)['provenance']['fingerprint'] != before


def test_theoretical_results_do_not_require_backtest_numbers():
    from reportagent.services.knowledge_cards import build_knowledge_card
    p = reviewed_paper()
    e = json.loads(p['evaluation_json']); e['research_type'] = 'theoretical'
    card = build_knowledge_card(p | {'evaluation_json': json.dumps(e)})
    assert card['status'] == 'ready' and card['results']
