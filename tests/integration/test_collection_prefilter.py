"""Automatic collection screens metadata before any PDF or database writes."""
import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.models.database import Base, Report
from reportagent.models.schemas import ClassificationResult, SearchResult, SourceType
from reportagent.services import collection
from reportagent.services.paper_scoring import screening_is_current
from reportagent.sources.arxiv_source import ArxivSource
from reportagent.sources import content_fetch


@pytest.fixture
def ingest(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path / "papers.db"}')
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    events, downloads = [], []
    class Model:
        fail = False
        calls = 0
        async def chat_json(self, messages, **kwargs):
            self.calls += 1
            events.append('score')
            if self.fail:
                raise RuntimeError('upstream failed token=do-not-log')
            content = messages[-1]['content']
            result = []
            for index, title in re.findall(r'### 论文 #(\d+)\n标题：([^\n]+)', content):
                score = 2 if 'Low' in title else 8
                result.append({'index': int(index), 'status': 'scored', 'score': score,
                    'relevance': score, 'best_direction': 'portfolio_optimization', 'reason': '摘要相关性评估'})
            return result
    model = Model()
    class Classifier:
        async def classify(self, title, abstract):
            events.append('classify')
            return ClassificationResult(method='fixture')
    source = ArxivSource(rate_limit_seconds=0)
    monkeypatch.setattr(content_fetch, 'PDF_DIRECTORY', tmp_path / 'accepted-pdfs')
    rows = [SearchResult(title=f'{kind} portfolio risk research', authors=['A'], source=SourceType.ARXIV,
        arxiv_id=f'2610.1000{n}v1', source_url=f'https://arxiv.org/abs/2610.1000{n}v1',
        abstract='We study portfolio allocation and compare out-of-sample financial risk models.',
        abstract_only=True, raw_metadata={'pdf_url': f'https://example.test/{kind}.pdf'})
        for n, kind in enumerate(('High', 'Low'), 1)]
    monkeypatch.setattr(source, '_execute_search', lambda *args, **kwargs: list(rows))
    def save(data, identifier, *, directory=None):
        folder = Path(directory) if directory is not None else tmp_path
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (identifier + '.pdf')
        path.write_bytes(data)
        return str(path)
    monkeypatch.setattr(source, '_save_pdf_bytes', save)
    import fitz
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_textbox(fitz.Rect(40, 40, 550, 800), 'Portfolio financial risk evaluation. ' * 15)
        data = pdf.tobytes()
    def respond(request):
        downloads.append(request.url.path)
        events.append('download')
        return httpx.Response(200, content=data)
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr(collection, 'get_session_factory', lambda: factory)
    monkeypatch.setattr(collection, 'init_db', lambda: None)
    monkeypatch.setattr(collection, 'build_sources', lambda names: [source])
    monkeypatch.setattr(collection, 'LLMClient', lambda: model)
    monkeypatch.setattr(collection, 'LLMClassifier', lambda _: Classifier())
    def run():
        return asyncio.run(collection.collect_reports(collection.CollectionOptions(
            topics='portfolio', sources='arxiv', days=0, no_analysis=True)))
    yield run, factory, rows, model, events, downloads, source
    engine.dispose()


def test_rejected_metadata_is_never_downloaded_or_persisted(ingest):
    run, factory, _, _, events, downloads, _ = ingest
    result = run()
    assert downloads == ['/High.pdf']
    assert events.index('score') < events.index('download')
    with factory() as session:
        stored = session.query(Report).all()
        assert len(stored) == 1 and stored[0].title.startswith('High')
        assert stored[0].selection_relevance == 8 and screening_is_current(stored[0])
        assert stored[0].has_full_text and Path(stored[0].pdf_path).is_file()
    assert result['storage']['added'] == 1 and result['results_count'] == 1
    assert result['source_statuses'][0]['screening']['rejected'] == 1
    assert 'Low portfolio' not in json.dumps(result)


def test_all_low_relevance_is_a_successful_empty_collection(ingest):
    run, factory, rows, _, _, downloads, _ = ingest
    rows[:] = [rows[1]]
    result = run()
    assert result['status'] == 'ok' and result['results_count'] == 0
    assert downloads == []
    with factory() as session:
        assert session.query(Report).count() == 0
    assert result['source_statuses'][0]['screening']['rejected'] == 1


def test_scoring_failure_keeps_library_clean_and_does_not_download(ingest):
    run, factory, _, model, _, downloads, _ = ingest
    model.fail = True
    result = run()
    assert downloads == [] and result['status'] != 'ok'
    with factory() as session:
        assert session.query(Report).count() == 0
    assert result['source_statuses'][0]['screening']['failed'] == 2
    assert 'do-not-log' not in json.dumps(result)


def test_overlapping_collection_reuses_current_score_and_full_text(ingest):
    run, factory, rows, model, _, downloads, _ = ingest
    rows[:] = [rows[0]]
    run()
    before = model.calls
    result = run()
    assert downloads == ['/High.pdf'] and model.calls == before
    with factory() as session:
        assert session.query(Report).count() == 1
        row = session.query(Report).one()
        assert row.has_full_text and Path(row.pdf_path).is_file()
        assert 'alphareport-prefilter-' not in row.pdf_path
        assert screening_is_current(session.query(Report).one())
    assert result['storage']['added'] == 0 and result['storage']['updated'] == 1


def test_pdf_failure_still_persists_only_accepted_metadata_and_score(ingest, monkeypatch):
    run, factory, _, _, _, _, source = ingest
    async def fail(result):
        raise httpx.ReadTimeout('private-request-data')
    monkeypatch.setattr(source, 'hydrate_result', fail, raising=False)
    result = run()
    assert result['status'] == 'partial'
    with factory() as session:
        row = session.query(Report).one()
        assert row.title.startswith('High') and row.selection_relevance == 8
        assert not row.has_full_text


def test_stale_scoring_cannot_authorize_download_after_rubric_changes(ingest, monkeypatch):
    from reportagent.services import paper_scoring
    run, factory, _, model, _, downloads, _ = ingest
    original = model.chat_json
    async def changed(messages, **kwargs):
        result = await original(messages, **kwargs)
        monkeypatch.setattr(paper_scoring, 'scoring_fingerprint', lambda: 'changed-rubric')
        return result
    monkeypatch.setattr(model, 'chat_json', changed)
    result = run()
    assert downloads == [] and result['status'] != 'ok'
    with factory() as session:
        assert session.query(Report).count() == 0


def test_threshold_increased_during_screening_cannot_authorize_download(ingest, monkeypatch):
    from reportagent.services import paper_scoring
    run, factory, rows, _, _, downloads, _ = ingest
    rows[:] = [rows[0]]
    load = paper_scoring._load_scoring_config
    calls = 0
    def changed():
        nonlocal calls
        calls += 1
        config = dict(load())
        config['push_settings'] = {**config.get('push_settings', {}),
                                   'llm_filter_threshold': 6 if calls == 1 else 9}
        return config
    monkeypatch.setattr(paper_scoring, '_load_scoring_config', changed)
    result = run()
    assert downloads == []
    with factory() as session:
        assert session.query(Report).count() == 0
    stats = result['source_statuses'][0]['screening']
    assert stats['selected'] == 0 and stats['rejected'] + stats['failed'] == 1


def test_policy_change_during_storage_rolls_back_new_metadata(ingest, monkeypatch):
    from reportagent.db.repository import ReportRepository
    from reportagent.services import paper_scoring
    run, factory, rows, _, _, downloads, _ = ingest
    rows[:] = [rows[0]]
    upsert = ReportRepository.upsert_report
    def changed(self, report, **kwargs):
        result = upsert(self, report, **kwargs)
        monkeypatch.setattr(paper_scoring, 'scoring_fingerprint', lambda: 'changed-during-storage')
        return result
    monkeypatch.setattr(ReportRepository, 'upsert_report', changed)
    result = run()
    assert downloads == [] and result['status'] != 'ok'
    with factory() as session:
        assert session.query(Report).count() == 0


def test_unavailable_model_does_not_download_even_to_fill_missing_abstract(ingest, monkeypatch):
    run, factory, rows, _, _, downloads, _ = ingest
    rows[:] = [rows[0].model_copy(update={'abstract': None})]
    def unavailable():
        raise RuntimeError('model unavailable')
    monkeypatch.setattr(collection, 'LLMClient', unavailable)
    result = run()
    assert downloads == [] and result['status'] != 'ok'
    with factory() as session:
        assert session.query(Report).count() == 0


def test_material_failure_with_non_quant_title_is_pending_not_rejected(ingest, monkeypatch):
    run, factory, rows, _, _, _, source = ingest
    rows[:] = [rows[0].model_copy(update={'title': 'Unknown study', 'abstract': None})]
    async def unchanged(result, **kwargs):
        return result
    monkeypatch.setattr(source, 'hydrate_result', unchanged)
    result = run()
    stats = result['source_statuses'][0]['screening']
    assert stats['pending'] == 1 and stats['rejected'] == 0 and result['status'] != 'ok'
    with factory() as session:
        assert session.query(Report).count() == 0


def test_model_result_cannot_overwrite_metadata_changed_during_scoring(ingest, monkeypatch):
    run, factory, rows, model, _, downloads, _ = ingest
    rows[:] = [rows[0]]
    run()
    with factory() as session:
        row = session.query(Report).one()
        row.selection_provenance_json = None  # force re-screening
        session.commit()
    original = model.chat_json
    async def changed(messages, **kwargs):
        with factory() as session:
            row = session.query(Report).one()
            row.title = 'A newer corrected title'
            row.abstract = 'Newer corrected research abstract.'
            row.selection_relevance = 2
            session.commit()
        return await original(messages, **kwargs)
    monkeypatch.setattr(model, 'chat_json', changed)
    result = run()
    with factory() as session:
        row = session.query(Report).one()
        assert row.title == 'A newer corrected title' and row.selection_relevance == 2
    assert downloads == ['/High.pdf'] and result['status'] != 'ok'


def test_missing_source_abstract_reuses_previously_acquired_material(ingest, monkeypatch):
    run, factory, rows, model, _, downloads, source = ingest
    rows[:] = [rows[0].model_copy(update={'abstract': None})]
    monkeypatch.setattr(source, '_parse_pdf_bytes', lambda _: 'Abstract\n'
        'We study portfolio allocation and compare out-of-sample financial risk models.\n1 Introduction\n' + 'Method details. ' * 20)
    result = run()
    assert result['results_count'] == 1
    with factory() as session:
        row = session.query(Report).one()
        assert row.has_full_text and Path(row.pdf_path).is_file()
        assert 'alphareport-prefilter-' not in row.pdf_path
    before = model.calls
    result = run()
    assert downloads == ['/High.pdf'] and model.calls == before
    with factory() as session:
        assert session.query(Report).count() == 1


def test_older_revision_is_skipped_without_scoring_or_cached_text_mixing(ingest):
    run, factory, rows, model, _, downloads, _ = ingest
    original = rows[0]
    rows[:] = [original.model_copy(update={'arxiv_id': '2610.10001v2'})]
    run()
    before = model.calls
    rows[:] = [original]
    result = run()
    assert downloads == ['/High.pdf'] and model.calls == before
    assert result['source_statuses'][0]['screening']['superseded'] == 1
    with factory() as session:
        assert session.query(Report).one().arxiv_id == '2610.10001v2'


def test_missing_abstract_later_rejected_leaves_no_pdf_or_library_record(ingest, monkeypatch):
    run, factory, rows, _, _, downloads, source = ingest
    rows[:] = [rows[1].model_copy(update={'abstract': None})]
    monkeypatch.setattr(source, '_parse_pdf_bytes', lambda _: 'Abstract\n'
        'We study portfolio allocation and compare out-of-sample financial risk models.\n1 Introduction\n' + 'Method details. ' * 20)
    result = run()
    assert result['source_statuses'][0]['screening']['rejected'] == 1
    assert downloads == ['/Low.pdf']  # only acquired to obtain the missing abstract
    folder = Path(factory.kw['bind'].url.database).parent
    assert list(folder.rglob('*.pdf')) == []
    with factory() as session:
        assert session.query(Report).count() == 0


def test_staged_pdf_is_removed_if_accepted_metadata_transaction_rolls_back(ingest, monkeypatch):
    from reportagent.db.repository import ReportRepository
    from reportagent.services import paper_scoring
    run, factory, rows, _, _, downloads, source = ingest
    rows[:] = [rows[0].model_copy(update={'abstract': None})]
    monkeypatch.setattr(source, '_parse_pdf_bytes', lambda _: 'Abstract\n'
        'We study portfolio allocation and compare out-of-sample financial risk models.\n1 Introduction\n' + 'Method details. ' * 20)
    upsert = ReportRepository.upsert_report
    def changed(self, report, **kwargs):
        result = upsert(self, report, **kwargs)
        monkeypatch.setattr(paper_scoring, 'scoring_fingerprint', lambda: 'changed-during-storage')
        return result
    monkeypatch.setattr(ReportRepository, 'upsert_report', changed)
    result = run()
    assert result['status'] != 'ok' and downloads == ['/High.pdf']
    folder = Path(factory.kw['bind'].url.database).parent
    assert list(folder.rglob('*.pdf')) == []
    with factory() as session:
        assert session.query(Report).count() == 0
