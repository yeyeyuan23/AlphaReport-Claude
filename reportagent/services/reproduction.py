"""Evidence extraction for reproduction preparation, distinct from proposed checks."""
from __future__ import annotations

import hashlib
import json
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

from reportagent.services.knowledge_cards import current_review, report_snapshot
from reportagent.services.text_evidence import locate_quote, text_chunks
from reportagent.skills.registry import skill_resource
from reportagent.utils.config import get_llm_model, get_llm_base_url, get_llm_provider

FIELDS = ('target', 'available_information', 'training_objective', 'data', 'split', 'baseline', 'metrics', 'assumptions')


class Excerpt(BaseModel):
    # Saved excerpts additionally contain recomputable raw offsets/locator metadata.
    model_config = ConfigDict(extra='ignore', str_strip_whitespace=True)
    quote: str = Field(min_length=20)
    chunk_id: str = Field(pattern=r'^C\d{3,}$')


class Fact(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    status: Literal['documented', 'needs_verification', 'not_applicable']
    value: str = Field(min_length=1)
    evidence: list[Excerpt]


class Preparation(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    fields: dict[str, Fact]
    minimum_check: str = Field(min_length=1)
    falsification: str = Field(min_length=1)


def reproduction_fingerprint(report) -> str:
    paper = report_snapshot(report)
    # Do not hash analysis_json: this card is stored inside it, and reads the original text.
    payload = {'schema': 1, 'paper': {k: v for k, v in paper.items() if k not in ('analysis_json', 'knowledge_card_json')},
               'model': get_llm_model(), 'provider': get_llm_provider(), 'endpoint': get_llm_base_url(),
               'prompts': {n: skill_resource('prepare-reproduction', n).read_text(encoding='utf-8')
                           for n in ('system.md', 'SKILL.md')}}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def validate_preparation(data: dict, text: str) -> dict:
    parsed = Preparation.model_validate({k: data[k] for k in ('fields', 'minimum_check', 'falsification')}).model_dump()
    if set(parsed['fields']) != set(FIELDS):
        raise ValueError('复现字段缺失或未知')
    for field in parsed['fields'].values():
        if field['status'] == 'documented' and not field['evidence']:
            raise ValueError('原文事实必须有证据')
        for excerpt in field['evidence']:
            located = locate_quote(text, excerpt['quote'], excerpt['chunk_id'])
            if located is None:
                raise ValueError('无法定位复现事实的原文引证')
            excerpt.update(located, verified=True)
    return parsed


def saved_preparation(report) -> dict:
    """Read without model calls; stale/invalid cards are never shown as current."""
    paper = report_snapshot(report)
    try:
        analysis = json.loads(paper.get('analysis_json') or '{}')
        cached = analysis.get('reproduction_preparation')
        if (current_review(paper) is not None and isinstance(cached, dict)
                and cached.get('status') == 'ready'
                and cached.get('fingerprint') == reproduction_fingerprint(paper)):
            return {**cached, **validate_preparation(cached, paper.get('full_text') or '')}
    except (ValueError, TypeError, KeyError, AttributeError):
        pass
    return {'status': 'pending', 'notice': '尚无当前版本的复现准备卡。生成前需完成全文证据评估。'}


async def prepare_reproduction(report, *, force=False, llm=None) -> dict:
    paper = report_snapshot(report)
    review = current_review(paper)
    if review is None:
        return {'status': 'pending', 'notice': '请先取得全文并完成当前版本的证据评估，再准备复现。'}
    fingerprint = reproduction_fingerprint(paper)
    text = paper.get('full_text') or ''
    try:
        analysis = json.loads(paper.get('analysis_json') or '{}')
        cached = analysis.get('reproduction_preparation') if isinstance(analysis, dict) else None
        if not force and isinstance(cached, dict) and cached.get('status') == 'ready' and cached.get('fingerprint') == fingerprint:
            checked = validate_preparation(cached, text)
            return {**cached, **checked}
    except (ValueError, TypeError, KeyError):
        pass
    if len(text) > 240000:
        return {'status': 'pending', 'notice': '全文超过本轮输入上限；请分批完整核对，未截断材料。'}
    try:
        if llm is None:
            from reportagent.llm.client import LLMClient
            llm = LLMClient()
        raw = await llm.chat_json([
            {'role': 'system', 'content': skill_resource('prepare-reproduction', 'system.md').read_text(encoding='utf-8')},
            {'role': 'user', 'content': json.dumps({'title': paper.get('title'), 'research_type': review['research_type'],
                                                  'text_chunks': text_chunks(text)}, ensure_ascii=False)},
        ], temperature=0.1, max_tokens=6500)
        result = validate_preparation(raw, text)
    except Exception:
        return {'status': 'retryable_error', 'notice': '生成失败、字段不完整或原文引证无效，请重试；旧准备卡未被覆盖。'}
    result.update(status='ready', fingerprint=fingerprint,
                  model=get_llm_model(), proposal_label='建议验证方案，非作者实验或已完成复现',
                  notice='以下是复现前的资料核对与建议，尚未复现论文实验。引文定位不代表结论已验证。',
                  open_questions=[name for name, fact in result['fields'].items() if fact['status'] == 'needs_verification'])
    return result


async def prepare_report(report_id: int, db, *, force=False, persist=True, llm=None) -> dict:
    """Shared API/CLI persistence with a source-change guard across the model call."""
    from reportagent.db.repository import ReportRepository
    report = ReportRepository(db).get_report(report_id)
    if report is None:
        raise LookupError('Report not found')
    before = reproduction_fingerprint(report)
    result = await prepare_reproduction(report, force=force, llm=llm)
    if result['status'] != 'ready' or not persist:
        return result
    db.refresh(report)
    if reproduction_fingerprint(report) != before:
        return {'status': 'retryable_error', 'notice': '生成期间原文或评估已变化，请重试；未保存过期准备卡。'}
    try:
        analysis = json.loads(report.analysis_json or '{}')
    except (ValueError, TypeError):
        analysis = {}
    if not isinstance(analysis, dict):
        analysis = {}
    analysis['reproduction_preparation'] = result
    report.analysis_json = json.dumps(analysis, ensure_ascii=False)
    db.commit()
    return result
