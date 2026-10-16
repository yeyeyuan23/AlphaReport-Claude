from __future__ import annotations
from typing import Optional

import asyncio
import logging
from datetime import datetime
from threading import Event

import httpx

from reportagent.models.schemas import UserCriteria, SearchResult, SourceType
from reportagent.sources.base import (
    BaseSource, SourceParseError, deduplicate_results, usable_full_text,
)
from reportagent.utils.topic_expansion import expand_topics

logger = logging.getLogger(__name__)


class ArxivSource(BaseSource):
    CATEGORY_MAP = {
        "risk model": ["q-fin.RM"],
        "risk management": ["q-fin.RM"],
        "risk parity": ["q-fin.RM", "q-fin.PM"],
        "value at risk": ["q-fin.RM"],
        "risk": ["q-fin.RM"],
        "portfolio optimization": ["q-fin.PM"],
        "asset allocation": ["q-fin.PM"],
        "mean-variance": ["q-fin.PM"],
        "markowitz": ["q-fin.PM"],
        "efficient frontier": ["q-fin.PM"],
        "rebalancing": ["q-fin.PM"],
        "portfolio": ["q-fin.PM"],
        "execution algorithm": ["q-fin.TR"],
        "algorithmic trading": ["q-fin.TR"],
        "optimal execution": ["q-fin.TR"],
        "transaction cost": ["q-fin.TR"],
        "twap": ["q-fin.TR"],
        "vwap": ["q-fin.TR"],
        "execution": ["q-fin.TR"],
        "high frequency trading": ["q-fin.TR", "q-fin.MF"],
        "hft": ["q-fin.TR", "q-fin.MF"],
        "market making": ["q-fin.TR", "q-fin.MF"],
        "low latency": ["q-fin.TR"],
        "machine learning": ["q-fin.ST", "stat.ML", "cs.LG"],
        "deep learning": ["q-fin.ST", "cs.LG"],
        "neural network": ["q-fin.ST", "cs.LG"],
        "reinforcement learning": ["q-fin.ST", "cs.LG"],
        "lstm": ["q-fin.ST", "cs.LG"],
        "transformer": ["q-fin.ST", "cs.LG"],
        "random forest": ["q-fin.ST", "stat.ML"],
        "xgboost": ["q-fin.ST", "stat.ML"],
        "factor model": ["q-fin.ST", "q-fin.PM"],
        "multi-factor": ["q-fin.ST", "q-fin.PM"],
        "factor investing": ["q-fin.ST", "q-fin.PM"],
        "smart beta": ["q-fin.ST", "q-fin.PM"],
        "fama-french": ["q-fin.ST", "q-fin.PM"],
        "factor": ["q-fin.ST", "q-fin.PM"],
        "volatility": ["q-fin.ST", "q-fin.MF"],
        "implied volatility": ["q-fin.PR", "q-fin.MF"],
        "stochastic volatility": ["q-fin.MF", "q-fin.PR"],
        "garch": ["q-fin.ST", "q-fin.MF"],
        "option pricing": ["q-fin.PR", "q-fin.CP"],
        "market microstructure": ["q-fin.TR", "q-fin.MF"],
        "order book": ["q-fin.TR", "q-fin.MF"],
        "limit order book": ["q-fin.TR", "q-fin.MF"],
        "price discovery": ["q-fin.TR", "q-fin.MF"],
        "statistical arbitrage": ["q-fin.ST", "q-fin.TR"],
        "pairs trading": ["q-fin.ST", "q-fin.TR"],
        "market neutral": ["q-fin.ST", "q-fin.TR"],
        "mean reversion": ["q-fin.ST"],
        "cointegration": ["q-fin.ST"],
        "option": ["q-fin.PR", "q-fin.CP"],
        "derivatives": ["q-fin.PR", "q-fin.CP"],
        "alternative data": ["q-fin.ST"],
        "sentiment analysis": ["q-fin.ST", "cs.CL"],
        "nlp finance": ["q-fin.ST", "cs.CL"],
        "text mining": ["q-fin.ST", "cs.CL"],
    }

    def __init__(self, rate_limit_seconds: float = 3.0, download_pdfs: bool = True):
        self.rate_limit_seconds = rate_limit_seconds
        self.download_pdfs = download_pdfs

    @property
    def source_type(self) -> SourceType:
        return SourceType.ARXIV

    def is_available(self) -> bool:
        return True

    async def search(self, criteria: UserCriteria) -> list[SearchResult]:
        """Standard keyword-based search (existing behavior)."""
        self._start_search()
        query = self._build_query(criteria)
        if not query:
            return []
        return await self._do_search(query, criteria)

    async def search_broad(self, criteria: UserCriteria) -> list[SearchResult]:
        """Broad search: pull ALL papers from q-fin categories + ML/Stat/AI/OC cross-category.

        No keyword restrictions — pulls everything in quant-finance arXiv categories
        plus finance-relevant papers from stat.ML / cs.LG / cs.CL / cs.AI / math.OC.
        Designed for "全量拉取 + LLM过滤" pipeline.

        Reference: QuantPaper/LLMQuant monitors q-fin.* + cs.LG + cs.AI + stat.ML + math.OC
        """
        self._start_search()
        # All 9 q-fin subcategories
        qfin_cats = [
            "q-fin.CP", "q-fin.EC", "q-fin.GN", "q-fin.MF",
            "q-fin.PM", "q-fin.PR", "q-fin.RM", "q-fin.ST", "q-fin.TR",
        ]
        cat_query = " OR ".join(f"cat:{c}" for c in qfin_cats)

        # Cross-category: ML/Stat/CL/AI/OC papers with finance relevance
        # cs.AI: 量化论文大量散落在 AI 分类（LLM-based trading, RL agent etc.）
        # math.OC: 组合优化、最优执行等方法论论文常在优化控制分类
        finance_terms = [
            "finance", "financial", "stock", "trading", "portfolio",
            "quantitative", "market", "volatility", "option", "risk",
            "asset", "equity", "factor",
        ]
        fin_term_query = " OR ".join(f'all:"{t}"' for t in finance_terms)
        cross_cats = ["stat.ML", "cs.LG", "cs.CL", "cs.AI", "math.OC"]
        cross_cat_query = " OR ".join(f"cat:{c}" for c in cross_cats)
        cross_query = f"({cross_cat_query}) AND ({fin_term_query})"

        query = f"({cat_query}) OR ({cross_query})"
        logger.info("Broad search query: %s", query[:200])
        return await self._do_search(query, criteria)

    async def _do_search(self, query: str, criteria: UserCriteria) -> list[SearchResult]:
        """Execute search with given query string."""
        partial_results: list[SearchResult] = []
        stop = Event()
        try:
            fetch_count = criteria.max_results_per_source
            if criteria.date_from or criteria.date_to:
                fetch_count = fetch_count * 5
            has_date_filter = bool(criteria.date_from or criteria.date_to)
            results = await asyncio.wait_for(
                asyncio.to_thread(
                    self._execute_search, query, fetch_count, sort_by_date=has_date_filter,
                    partial_results=partial_results, stop=stop
                ),
                timeout=120,
            )
        except Exception as error:
            stop.set()
            logger.warning("ArxivSource %s", self.last_search.record_error(error, "search"))
            results = list(partial_results)

        self.last_search.fetched = max(self.last_search.fetched, len(results))
        results = deduplicate_results(results)
        if criteria.date_from or criteria.date_to:
            results = self._filter_by_date(results, criteria.date_from, criteria.date_to)
            results = results[:criteria.max_results_per_source]

        if self.download_pdfs and results:
            results = await self._download_and_parse_pdfs(results)

        return results

    async def _download_and_parse_pdfs(self, results: list[SearchResult]) -> list[SearchResult]:
        import httpx

        updated = []
        timeout = httpx.Timeout(connect=15.0, read=30.0, write=10.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            for r in results:
                pdf_url = r.raw_metadata.get("pdf_url")
                if not r.abstract_only:
                    updated.append(r)
                    continue
                if not pdf_url:
                    self.last_search.record_error(SourceParseError("missing PDF URL"), "PDF download")
                    updated.append(r)
                    continue

                try:
                    r = await asyncio.wait_for(
                        self._fetch_one_pdf(client, r, pdf_url), timeout=45
                    )
                except Exception as error:
                    logger.warning("ArxivSource %s", self.last_search.record_error(error, "PDF download/parse"))

                updated.append(r)

        return updated

    async def _fetch_one_pdf(
        self, client, result: SearchResult, pdf_url: str
    ) -> SearchResult:
        resp = await client.get(pdf_url)
        resp.raise_for_status()
        # Read content with explicit timeout protection
        pdf_bytes = await asyncio.wait_for(
            asyncio.to_thread(lambda: resp.content), timeout=15
        )

        if not pdf_bytes[:1024].lstrip().startswith(b"%PDF-"):
            raise SourceParseError("non-PDF response content")
        saved_path = await asyncio.to_thread(self._save_pdf_bytes, pdf_bytes, result.arxiv_id)
        if result.arxiv_id and not saved_path:
            self.last_search.record_error(SourceParseError("PDF could not be saved"), "PDF save")

        # Text extraction is optional — skip on timeout to avoid blocking
        try:
            full_text = await asyncio.wait_for(
                asyncio.to_thread(self._parse_pdf_bytes, pdf_bytes), timeout=20
            )
        except asyncio.TimeoutError as error:
            self.last_search.record_error(error, "PDF text extraction")
            full_text = ""

        updates: dict = {}
        if saved_path:
            updates["pdf_path"] = saved_path
        if usable_full_text(full_text):
            updates["full_text"] = full_text
            updates["abstract_only"] = False
        else:
            self.last_search.record_error(SourceParseError("PDF has no usable extracted text"), "PDF parse")

        if updates:
            result = result.model_copy(update=updates)

        return result

    @staticmethod
    def _save_pdf_bytes(pdf_bytes: bytes, arxiv_id  : Optional[str]) -> Optional[str]:
        if not arxiv_id:
            return None
        try:
            from reportagent.utils.config import PROJECT_ROOT
            pdf_dir = PROJECT_ROOT / "data" / "pdfs"
            pdf_dir.mkdir(parents=True, exist_ok=True)
            safe_name = arxiv_id.replace("/", "_").replace("\\", "_")
            pdf_path = pdf_dir / f"{safe_name}.pdf"
            pdf_path.write_bytes(pdf_bytes)
            return str(pdf_path)
        except Exception as e:
            logger.warning("Failed to save PDF: %s", type(e).__name__)
            return None

    @staticmethod
    def _parse_pdf_bytes(pdf_bytes: bytes) -> Optional[str]:
        try:
            import fitz
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            try:
                text = "\n".join(page.get_text() for page in doc)
                if text and len(text.strip()) > 100:
                    return text
            finally:
                doc.close()
        except Exception as e:
            logger.debug("PyMuPDF parse failed: %s", type(e).__name__)
        return None

    def _filter_by_date(
        self, results: list[SearchResult], date_from, date_to
    ) -> list[SearchResult]:
        filtered = []
        for r in results:
            if not r.published_date:
                filtered.append(r)
                continue
            # Normalize both to naive UTC for comparison
            if r.published_date.tzinfo:
                pub = r.published_date.replace(tzinfo=None)
            else:
                pub = r.published_date
            df = date_from.replace(tzinfo=None) if date_from and date_from.tzinfo else date_from
            dt = date_to.replace(tzinfo=None) if date_to and date_to.tzinfo else date_to
            if df and pub < df:
                continue
            if dt and pub > dt:
                continue
            filtered.append(r)
        return filtered

    def _build_query(self, criteria: UserCriteria) -> str:
        en_terms = expand_topics(criteria.topics, criteria.keywords, lang="en")

        topic_parts = []
        categories: set[str] = set()
        for term in en_terms:
            topic_parts.append(f'all:"{term}"')
            term_lower = term.lower()
            for key, cats in self.CATEGORY_MAP.items():
                if key in term_lower or term_lower in key:
                    categories.update(cats)

        parts = []
        if topic_parts:
            parts.append("(" + " OR ".join(topic_parts) + ")")

        if categories:
            cat_parts = [f"cat:{c}" for c in sorted(categories)]
            parts.append("(" + " OR ".join(cat_parts) + ")")

        return " AND ".join(parts) if parts else ""

    def _execute_search(
        self, query: str, max_results: int, sort_by_date: bool = False,
        partial_results: list[SearchResult] | None = None, stop: Event | None = None,
    ) -> list[SearchResult]:
        from xml.etree import ElementTree as ET

        diagnostics = self.last_search
        atom = "{http://www.w3.org/2005/Atom}"
        results = partial_results if partial_results is not None else []
        stop = stop or Event()
        offset = 0
        # Parse actual responses here: the SDK silently skips some malformed entries
        # but raises on others, losing their valid siblings and hiding raw counts.
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            while offset < max_results and not stop.is_set():
                try:
                    response = client.get("https://export.arxiv.org/api/query", params={
                        "search_query": query, "start": offset,
                        "max_results": min(100, max_results - offset),
                        "sortBy": "submittedDate" if sort_by_date else "relevance",
                        "sortOrder": "descending",
                    })
                    if stop.is_set():
                        break
                    response.raise_for_status()
                    if b"<!DOCTYPE" in response.content.upper():
                        raise SourceParseError("unexpected XML document type")
                    try:
                        root = ET.fromstring(response.content)
                    except ET.ParseError as error:
                        raise SourceParseError("malformed Atom XML") from error
                    if root.tag != atom + "feed":
                        raise SourceParseError("non-Atom response content")
                    total = root.findtext("{http://a9.com/-/spec/opensearch/1.1/}totalResults")
                    if total is None or not total.strip().isdigit():
                        raise SourceParseError("Atom feed missing valid totalResults")
                    entries = root.findall(atom + "entry")
                    diagnostics.fetched += len(entries)
                    if not entries and offset < int(total):
                        raise SourceParseError("Atom feed missing expected entries")
                    parsed = 0
                    for entry in entries:
                        try:
                            results.append(self._parse_atom_entry(entry))
                            parsed += 1
                        except Exception as error:
                            diagnostics.record_error(error, "Atom entry")
                    if parsed or not entries:
                        diagnostics.successful_requests += 1
                    # Advance by raw entries, including invalid ones, so a bad item
                    # cannot shift subsequent pages or cause an endless retry loop.
                    offset += len(entries)
                    if not entries or offset >= int(total) or offset >= max_results:
                        break
                    stop.wait(self.rate_limit_seconds)
                except Exception as error:
                    logger.warning("ArxivSource %s", diagnostics.record_error(error, "search"))
                    break  # Preserve all papers obtained before this page failed.
        return results[:max_results]

    @staticmethod
    def _parse_atom_entry(entry) -> SearchResult:
        atom = "{http://www.w3.org/2005/Atom}"
        arxiv_ns = "{http://arxiv.org/schemas/atom}"
        values = {name: (entry.findtext(atom + name) or "").strip()
                  for name in ("id", "title", "updated", "published")}
        if not all(values.values()) or "/abs/" not in values["id"]:
            raise SourceParseError("Atom entry missing required fields or paper identifier")
        try:
            published = datetime.fromisoformat(values["published"].replace("Z", "+00:00"))
            updated = datetime.fromisoformat(values["updated"].replace("Z", "+00:00"))
        except ValueError as error:
            raise SourceParseError("Atom entry has invalid publication date") from error
        pdf_url = next((link.get("href") for link in entry.findall(atom + "link")
                        if link.get("title") == "pdf" or link.get("type") == "application/pdf"), None)
        primary = entry.find(arxiv_ns + "primary_category")
        return SearchResult(
            title=" ".join(values["title"].split()),
            authors=[author.findtext(atom + "name") or "" for author in entry.findall(atom + "author")],
            abstract=(entry.findtext(atom + "summary") or "").strip(),
            full_text=None, abstract_only=True, source=SourceType.ARXIV,
            source_url=values["id"], arxiv_id=values["id"].split("/abs/", 1)[1],
            doi=entry.findtext(arxiv_ns + "doi"), published_date=updated or published,
            raw_metadata={
                "categories": [category.get("term", "") for category in entry.findall(atom + "category")],
                "primary_category": primary.get("term", "") if primary is not None else "",
                "pdf_url": pdf_url,
            },
        )
