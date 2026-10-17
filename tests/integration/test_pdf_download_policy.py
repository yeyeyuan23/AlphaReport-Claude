"""PDF resource budgets, retries and diagnostics without external requests."""
import asyncio
import json
import logging
import time

import httpx
import pytest

from reportagent.models.schemas import SearchResult, SourceType
from reportagent.sources import content_fetch as fetch
from reportagent.sources.arxiv_source import ArxivSource
from reportagent.sources.base import SearchDiagnostics


@pytest.fixture
def policy(monkeypatch, tmp_path):
    values = {'max_size_mb': 25, 'download_timeout_seconds': .2,
              'parse_timeout_seconds': .2, 'max_attempts': 3,
              'retry_backoff_seconds': .001, 'max_retry_delay_seconds': .01}
    monkeypatch.setattr(fetch, 'get_config', lambda *args, **kwargs: values, raising=False)
    monkeypatch.setattr(fetch, 'PDF_DIRECTORY', tmp_path)
    return values


@pytest.fixture
def document():
    import fitz
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_textbox(fitz.Rect(40, 40, 550, 800), 'Portfolio risk and financial evaluation. ' * 12)
        return pdf.tobytes()


async def hydrate(handler):
    result = SearchResult(title='Portfolio study', source=SourceType.BIGQUANT,
                          source_url='https://example.test/paper', abstract_only=True)
    diagnostics = SearchDiagnostics()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        row = await fetch.hydrate_pdf(client, result, ['https://example.test/paper.pdf?token=do-not-log'], diagnostics)
    return row, diagnostics


def project_logs(caplog):
    return '\n'.join(record.getMessage() for record in caplog.records
                     if record.name.startswith('reportagent.'))


def test_configured_size_limit_is_enforced_without_retry(policy, document):
    policy['max_size_mb'] = .0001
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=document)
    row, diagnostics = asyncio.run(hydrate(respond))
    assert row.abstract_only and row.pdf_path is None
    assert len(requests) == 1
    assert 'size limit' in ' '.join(diagnostics.errors)


@pytest.mark.parametrize('failure', ['timeout', 'connection', 429, 503])
def test_transient_download_failure_recovers_without_repeating_parse(policy, document, failure, caplog):
    requests = []
    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            if failure == 'timeout':
                raise httpx.ReadTimeout('sensitive query token=do-not-log', request=request)
            if failure == 'connection':
                raise httpx.ConnectError('sensitive query token=do-not-log', request=request)
            return httpx.Response(failure, headers={'Retry-After': '0'})
        return httpx.Response(200, content=document)
    with caplog.at_level(logging.INFO):
        row, diagnostics = asyncio.run(hydrate(respond))
    assert not row.abstract_only and row.full_text and row.pdf_path
    assert diagnostics.errors == []
    assert len(requests) == 2
    assert 'retry' in project_logs(caplog) and 'do-not-log' not in project_logs(caplog)


@pytest.mark.parametrize('status', [401, 403, 404])
def test_permanent_http_failure_is_not_retried(policy, status, caplog):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(status)
    with caplog.at_level(logging.INFO):
        row, diagnostics = asyncio.run(hydrate(respond))
    assert row.abstract_only and len(requests) == 1
    assert f'HTTP {status}' in ' '.join(diagnostics.errors)
    assert 'do-not-log' not in project_logs(caplog) + str(diagnostics.errors)


def test_retry_exhaustion_reports_stage_identity_attempt_and_bytes(policy, caplog):
    policy['max_attempts'] = 2
    calls = []
    def respond(request):
        calls.append(request)
        raise httpx.ReadTimeout('token=do-not-log', request=request)
    with caplog.at_level(logging.INFO):
        row, diagnostics = asyncio.run(hydrate(respond))
    assert row.abstract_only and len(calls) == 2
    error = ' '.join(diagnostics.errors)
    for field in ('download', 'paper=', 'attempt=2', 'bytes=0', 'elapsed_ms=', 'ReadTimeout'):
        assert field in error
    assert 'do-not-log' not in project_logs(caplog) + error


def test_configured_parse_timeout_keeps_arxiv_pdf_and_does_not_redownload(policy, document, monkeypatch, tmp_path):
    policy['parse_timeout_seconds'] = .005
    source = ArxivSource()
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=document)
    def slow_parse(data):
        time.sleep(.04)
        return 'Portfolio analysis. ' * 20
    path = tmp_path / 'paper.pdf'
    monkeypatch.setattr(source, '_parse_pdf_bytes', slow_parse)
    monkeypatch.setattr(source, '_save_pdf_bytes', lambda data, identifier: (path.write_bytes(data), str(path))[1])
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await source._fetch_one_pdf(client, SearchResult(title='Paper', source=SourceType.ARXIV,
                arxiv_id='2610.12345v1', abstract='Original abstract', abstract_only=True), 'https://example.test/paper.pdf')
    row = asyncio.run(run())
    assert row.abstract_only and row.full_text is None and path.is_file()
    assert row.pdf_path == str(path) and row.abstract == 'Original abstract'
    assert len(requests) == 1
    error = ' '.join(source.last_search.errors)
    assert 'parse' in error and '2610.12345v1' in error and 'elapsed_ms=' in error


def test_download_and_parse_use_separate_budgets(policy, document, caplog):
    # The parse budget must not accidentally use the short download deadline.
    policy['download_timeout_seconds'] = .01
    with caplog.at_level(logging.INFO):
        row, diagnostics = asyncio.run(hydrate(lambda _: httpx.Response(200, content=document)))
    assert not row.abstract_only and diagnostics.errors == []
    events = [json.loads(record.message.split('pdf_transfer ', 1)[1]) for record in caplog.records
              if 'pdf_transfer ' in record.message]
    for stage in ('download', 'parse', 'save'):
        event = next(e for e in events if e['stage'] == stage and e['status'] == 'ok')
        assert event['source'] == 'bigquant' and event['paper_id']
        assert event['bytes'] == len(document) and event['elapsed_ms'] >= 0


@pytest.mark.parametrize('key,value', [('max_size_mb', 0), ('download_timeout_seconds', -1),
    ('parse_timeout_seconds', float('nan')), ('max_attempts', 0), ('max_attempts', True)])
def test_invalid_policy_is_rejected_before_network_io(policy, key, value):
    policy[key] = value
    calls = []
    with pytest.raises(ValueError):
        asyncio.run(hydrate(lambda request: calls.append(request) or httpx.Response(404)))
    assert calls == []


def test_partial_stream_is_discarded_before_retry(policy, document, caplog):
    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'%PDF-partial'
            raise httpx.ReadError('do-not-log')
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, stream=Interrupted()) if len(calls) == 1 else httpx.Response(200, content=document)
    with caplog.at_level(logging.INFO):
        row, diagnostics = asyncio.run(hydrate(respond))
    assert row.pdf_path and diagnostics.errors == [] and len(calls) == 2
    from pathlib import Path
    assert Path(row.pdf_path).read_bytes() == document
    events = [json.loads(record.message.split('pdf_transfer ', 1)[1]) for record in caplog.records
              if 'pdf_transfer ' in record.message]
    assert next(e for e in events if e['status'] == 'retry')['bytes'] == 12


def test_unknown_length_stream_cannot_bypass_size_limit(policy):
    policy['max_size_mb'] = 100 / 1024 / 1024
    class Chunked(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'%PDF-' + b'x' * 45
            yield b'x' * 51
    row, diagnostics = asyncio.run(hydrate(lambda _: httpx.Response(200, stream=Chunked())))
    assert row.pdf_path is None and row.abstract_only
    assert 'size limit' in ' '.join(diagnostics.errors) and 'bytes=101' in ' '.join(diagnostics.errors)


def test_configured_wall_timeout_is_retried_and_identified(policy):
    policy['download_timeout_seconds'] = .005
    policy['max_attempts'] = 2
    calls = []
    class Stalled(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'%PDF-'
            await asyncio.sleep(.03)
            yield b'end'
    def respond(request):
        calls.append(request)
        return httpx.Response(200, stream=Stalled())
    row, diagnostics = asyncio.run(hydrate(respond))
    assert row.abstract_only and len(calls) == 2
    assert 'download' in ' '.join(diagnostics.errors)
    assert 'TimeoutError' in ' '.join(diagnostics.errors) and 'bytes=5' in ' '.join(diagnostics.errors)


def test_retry_after_is_honored_with_a_bounded_delay(policy, document, monkeypatch, caplog):
    delays, calls = [], []
    async def sleep(seconds):
        delays.append(seconds)
    monkeypatch.setattr(fetch.asyncio, 'sleep', sleep)
    def respond(request):
        calls.append(request)
        return httpx.Response(429, headers={'Retry-After': '600'}) if len(calls) == 1 else httpx.Response(200, content=document)
    with caplog.at_level(logging.INFO):
        row, _ = asyncio.run(hydrate(respond))
    assert row.pdf_path and delays == [.01]
    event = next(json.loads(r.message.split('pdf_transfer ', 1)[1]) for r in caplog.records
                 if 'pdf_transfer ' in r.message and '"status": "retry"' in r.message)
    assert event['retry_delay_seconds'] == .01


def test_pdf_request_uses_configured_timeout_instead_of_client_default(policy, document):
    captured = []
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
                captured.append(request.extensions['timeout']) or httpx.Response(200, content=document)), timeout=.001) as client:
            return await fetch.hydrate_pdf(client, SearchResult(title='P', source=SourceType.BIGQUANT),
                                          ['https://example.test/paper.pdf'], SearchDiagnostics())
    row = asyncio.run(run())
    assert row.pdf_path
    assert captured[0]['read'] == .2


def test_valid_image_only_pdf_is_cached_with_a_specific_parse_diagnostic(policy):
    import fitz
    from pathlib import Path
    with fitz.open() as pdf:
        pdf.new_page()
        data = pdf.tobytes()
    requests = []
    row, diagnostics = asyncio.run(hydrate(lambda request: requests.append(request) or httpx.Response(200, content=data)))
    assert row.abstract_only and not row.full_text and len(requests) == 1
    assert row.pdf_path and Path(row.pdf_path).read_bytes() == data
    assert row.raw_metadata['content_kind'] == 'pdf_without_text'
    assert 'no usable extracted text' in ' '.join(diagnostics.errors)
    assert 'paper=' in ' '.join(diagnostics.errors)
