"""Construct configured sources for explicit collection and default agent workflows."""

from __future__ import annotations

from reportagent.sources.arxiv_source import ArxivSource
from reportagent.sources.base import BaseSource
from reportagent.sources.bigquant_source import BigQuantSource
from reportagent.sources.eastmoney_source import EastMoneySource
from reportagent.sources.local_pdf import LocalPDFSource
from reportagent.utils.config import get_config


DEFAULT_SOURCE_NAMES = ("local_pdf", "arxiv", "eastmoney", "bigquant")


def build_sources(source_names: list[str] | None = None) -> list[BaseSource]:
    """Honor enabled flags for defaults; explicit names keep their order and scope.

    ``download_pdfs`` controls each source's eager search downloads. Collection
    may still hydrate selected results explicitly after its relevance prefilter.

    Unknown explicit names are skipped for compatibility with collection's own
    validation. Supplying an empty list selects nothing, rather than the defaults.
    """
    if source_names is None:
        source_names = [
            name
            for name in DEFAULT_SOURCE_NAMES
            if get_config("sources", name, "enabled", default=True)
        ]

    sources: list[BaseSource] = []
    for name in source_names:
        if name == "local_pdf":
            sources.append(
                LocalPDFSource(
                    get_config(
                        "sources", name, "library_path", default="data/pdf_library"
                    )
                )
            )
        elif name == "arxiv":
            sources.append(
                ArxivSource(
                    rate_limit_seconds=get_config(
                        "sources", name, "rate_limit_seconds", default=3.0
                    ),
                    download_pdfs=get_config(
                        "sources", name, "download_pdfs", default=True
                    ),
                )
            )
        elif name == "eastmoney":
            sources.append(
                EastMoneySource(
                    rate_limit_seconds=get_config(
                        "sources", name, "rate_limit_seconds", default=1.0
                    ),
                    download_pdfs=get_config(
                        "sources", name, "download_pdfs", default=True
                    ),
                )
            )
        elif name == "bigquant":
            sources.append(
                BigQuantSource(
                    rate_limit_seconds=get_config(
                        "sources", name, "rate_limit_seconds", default=2.0
                    ),
                    max_listing_pages=get_config(
                        "sources", name, "max_pages", default=10
                    ),
                    download_pdfs=get_config(
                        "sources", name, "download_pdfs", default=True
                    ),
                )
            )
    return sources
