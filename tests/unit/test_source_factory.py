"""Collection and default source construction share configuration semantics."""

import asyncio
import importlib
import importlib.util
from pathlib import Path

import pytest

from reportagent.services import collection
from reportagent.sources import factory as source_factory
from reportagent.sources.arxiv_source import ArxivSource
from reportagent.sources.bigquant_source import BigQuantSource
from reportagent.sources.eastmoney_source import EastMoneySource
from reportagent.sources.local_pdf import LocalPDFSource
from reportagent.utils import config


SOURCE_NAMES = ["local_pdf", "arxiv", "eastmoney", "bigquant"]


@pytest.fixture
def build_sources():
    return lambda: collection.build_sources(SOURCE_NAMES)


def configure(monkeypatch, sources):
    monkeypatch.setattr(config, "load_config", lambda: {"sources": sources})


def shared_factory():
    module = "reportagent.sources.factory"
    assert importlib.util.find_spec(module), "Source configuration needs one factory"
    return importlib.import_module(module)


def test_both_callers_apply_configured_constructor_options(
    build_sources, monkeypatch, tmp_path
):
    library = tmp_path / "papers with spaces" / "未下载"
    configure(
        monkeypatch,
        {
            "local_pdf": {"library_path": str(library)},
            "arxiv": {"rate_limit_seconds": 0, "download_pdfs": False},
            "eastmoney": {"rate_limit_seconds": 0.25, "download_pdfs": False},
            "bigquant": {
                "rate_limit_seconds": 0.5,
                "max_pages": 0,
                "download_pdfs": False,
            },
        },
    )

    local, arxiv, eastmoney, bigquant = build_sources()

    assert isinstance(local, LocalPDFSource)
    assert local.pdf_library_path == library
    assert not library.exists(), "Constructing a source must not create its library"
    assert isinstance(arxiv, ArxivSource)
    assert arxiv.rate_limit_seconds == 0
    assert arxiv.download_pdfs is False
    assert isinstance(eastmoney, EastMoneySource)
    assert eastmoney.rate_limit_seconds == 0.25
    assert eastmoney.download_pdfs is False
    assert isinstance(bigquant, BigQuantSource)
    assert bigquant.rate_limit_seconds == 0.5
    assert bigquant.max_listing_pages == 0
    assert bigquant.download_pdfs is False


def test_both_callers_preserve_missing_configuration_defaults(
    build_sources, monkeypatch
):
    configure(monkeypatch, {})

    local, arxiv, eastmoney, bigquant = build_sources()

    assert [s.source_type.value for s in (local, arxiv, eastmoney, bigquant)] == (
        SOURCE_NAMES
    )
    assert local.pdf_library_path == Path("data/pdf_library")
    assert arxiv.rate_limit_seconds == 3.0
    assert eastmoney.rate_limit_seconds == 1.0
    assert bigquant.rate_limit_seconds == 2.0
    assert bigquant.max_listing_pages == 10
    assert all(s.download_pdfs is True for s in (arxiv, eastmoney, bigquant))


def test_explicit_sources_override_enabled_but_defaults_honor_it(monkeypatch):
    configure(monkeypatch, {name: {"enabled": False} for name in SOURCE_NAMES})

    assert [s.source_type.value for s in collection.build_sources(SOURCE_NAMES)] == (
        SOURCE_NAMES
    )
    assert source_factory.build_sources() == []


def test_defaults_keep_order_and_ignore_unknown_config(monkeypatch):
    configure(
        monkeypatch,
        {
            "bigquant": {"enabled": True},
            "unknown": {"enabled": True},
            "arxiv": {"enabled": False},
        },
    )

    assert [s.source_type.value for s in source_factory.build_sources()] == [
        "local_pdf",
        "eastmoney",
        "bigquant",
    ]


def test_explicit_selection_keeps_order_duplicates_and_unknown_name_behavior(
    monkeypatch,
):
    configure(monkeypatch, {})

    sources = collection.build_sources(["bigquant", "unknown", "arxiv", "bigquant"])

    assert [s.source_type.value for s in sources] == ["bigquant", "arxiv", "bigquant"]
    assert sources[0] is not sources[2]
    assert collection.build_sources([]) == []
    assert collection.build_sources(["unknown", "ARXIV"]) == []


@pytest.mark.parametrize("path", ["./papers/../library", "~/papers"])
def test_both_callers_keep_local_path_semantics(build_sources, monkeypatch, path):
    configure(monkeypatch, {"local_pdf": {"library_path": path}})

    assert build_sources()[0].pdf_library_path == Path(path)


def test_collection_delegates_to_shared_factory(monkeypatch):
    factory = shared_factory()
    expected = [object()]
    calls = []

    def build(names=None):
        calls.append(names)
        return expected

    monkeypatch.setattr(factory, "build_sources", build)
    names = ["arxiv", "unknown"]
    assert collection.build_sources(names) is expected
    assert calls == [names]


def test_collection_workflow_uses_shared_factory_and_keeps_no_valid_sources_error(
    monkeypatch,
):
    factory = shared_factory()
    selected = []

    def build(names):
        selected.extend(names)
        return []

    monkeypatch.setattr(factory, "build_sources", build)
    monkeypatch.setattr(collection, "init_db", lambda: None)

    with pytest.raises(ValueError, match="^No valid sources specified$"):
        asyncio.run(
            collection.collect_reports(
                collection.CollectionOptions(topics="portfolio", sources=" arxiv ")
            )
        )
    assert selected == ["arxiv"]


@pytest.mark.parametrize("sources", ["unknown", "arxiv,unknown"])
def test_collection_preserves_unknown_source_errors(monkeypatch, sources):
    configure(monkeypatch, {})
    monkeypatch.setattr(collection, "init_db", lambda: None)
    expected = (
        "No valid sources specified"
        if sources == "unknown"
        else "'unknown' is not a valid SourceType"
    )

    with pytest.raises(ValueError) as error:
        asyncio.run(
            collection.collect_reports(
                collection.CollectionOptions(topics="portfolio", sources=sources)
            )
        )

    assert str(error.value) == expected
