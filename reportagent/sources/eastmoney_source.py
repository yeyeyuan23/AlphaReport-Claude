from __future__ import annotations

import asyncio
from hashlib import sha256
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from reportagent.models.schemas import UserCriteria, SearchResult, SourceType
from reportagent.sources.base import (
    BaseSource,
    SourceParseError,
    deduplicate_results,
    usable_full_text,
)
from reportagent.sources.date_window import candidate_in_window, utc_datetime
from reportagent.sources.content_fetch import (
    MAX_HTML_BYTES,
    article_text,
    bounded_download,
    hydrate_pdf,
    pdf_links,
)
from reportagent.utils.topic_expansion import expand_topics

logger = logging.getLogger(__name__)

REPORT_TYPES = {
    "stock": 0,
    "industry": 1,
    "deep": 2,
}

REPORT_TYPE_LABELS = {
    0: "个股研报",
    1: "行业研报",
    2: "深度研报",
}

TOPIC_TO_QTYPES = {
    "risk model": [2],
    "风险模型": [2],
    "风控": [2],
    "factor": [2, 1],
    "因子": [2, 1],
    "多因子": [2, 1],
    "alpha": [2],
    "选股": [2],
    "execution": [2],
    "执行算法": [2],
    "算法交易": [2],
    "portfolio": [2],
    "组合优化": [2],
    "资产配置": [2],
    "strategy": [2],
    "策略": [2],
    "高频": [2],
    "high frequency": [2],
    "量化": [2, 1],
    "quantitative": [2, 1],
    "machine learning": [2],
    "机器学习": [2],
    "深度学习": [2],
    "金融工程": [2, 1],
    "金工": [2, 1],
    "CTA": [2],
    "波动率": [2],
    "套利": [2],
    "对冲": [2],
    "回测": [2],
    "微观结构": [2],
    "指数增强": [2],
    "市场中性": [2],
}

QUANT_KEYWORDS = [
    kw.lower()
    for kw in [
        "量化策略",
        "量化选股",
        "量化投资",
        "量化模型",
        "量化配置",
        "量化对冲",
        "主动量化",
        "宏观量化",
        "量化经济",
        "量化周报",
        "金工",
        "金融工程",
        "因子模型",
        "多因子",
        "因子选股",
        "因子投资",
        "因子配置",
        "因子周报",
        "因子策略",
        "因子表现",
        "因子收益",
        "选股模型",
        "选股策略",
        "统计套利",
        "算法交易",
        "程序化交易",
        "CTA",
        "管理期货",
        "组合优化",
        "波动率模型",
        "期权定价",
        "期权策略",
        "风险平价",
        "风险预算",
        "Smart Beta",
        "smartbeta",
        "配对交易",
        "市场微观结构",
        "高频交易",
        "高频策略",
        "指数增强",
        "市场中性",
        "绝对收益",
        "Black-Litterman",
        "均值方差",
        "机器学习选股",
        "深度学习策略",
        "factor model",
        "arbitrage",
        "HFT",
        "backtest",
        "risk parity",
        "mean-variance",
    ]
]

EXCLUDE_PATTERNS = ["量化宽松", "轻量化", "减量化"]

AMBIGUOUS_TERMS = {"量化", "quant", "alpha", "因子"}

LIST_API = "https://reportapi.eastmoney.com/report/list"
DETAIL_BASE = "https://data.eastmoney.com/report/info/{info_code}.html"

FETCH_PAGES_PER_QTYPE = 8
FETCH_PAGE_SIZE = 100


class EastMoneySource(BaseSource):
    def __init__(self, rate_limit_seconds: float = 1.0, download_pdfs: bool = True):
        self.rate_limit_seconds = rate_limit_seconds
        self.download_pdfs = download_pdfs

    @property
    def source_type(self) -> SourceType:
        return SourceType.EASTMONEY

    def is_available(self) -> bool:
        return True

    async def search(self, criteria: UserCriteria) -> list[SearchResult]:
        self._start_search()
        q_types = self._resolve_qtypes(criteria)
        date_from, date_to = self._resolve_dates(criteria)

        all_results: list[SearchResult] = []

        async with httpx.AsyncClient(timeout=30.0) as client:
            for qt in q_types:
                try:
                    results = await self._fetch_reports(
                        client,
                        qt,
                        date_from,
                        date_to,
                        None
                        if criteria.max_results_per_source is None
                        else FETCH_PAGE_SIZE * FETCH_PAGES_PER_QTYPE,
                    )
                    all_results.extend(results)
                    await asyncio.sleep(self.rate_limit_seconds)
                except Exception as e:
                    logger.warning(
                        "EastMoney %s", self.last_search.record_error(e, f"qType {qt}")
                    )

        dated = []
        for row in deduplicate_results(all_results):
            stamp = (
                row.announced_at
                if criteria.date_basis == "announcement"
                else row.published_date
            )
            if criteria.date_basis == "announcement" and stamp is None:
                self.last_search.record_error(
                    SourceParseError("announcement date unavailable"), "report date"
                )
                continue
            if not candidate_in_window(row, criteria):
                continue
            dated.append(row)
        # Announcement collection delegates relevance to the shared abstract
        # assessment; titles and report categories must not discard candidates.
        filtered = (
            dated
            if criteria.date_basis == "announcement"
            else self._filter_by_keywords(dated, criteria)
        )
        logger.info(
            "EastMoney: fetched %d, retained %d eligible candidates",
            len(all_results),
            len(filtered),
        )
        selected = filtered[: criteria.max_results_per_source]
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            for index, result in enumerate(selected):
                try:
                    selected[index] = await self._fetch_detail(client, result)
                except Exception as error:
                    self.last_search.record_error(error, "article detail")
        return selected

    async def hydrate_result(
        self,
        result: SearchResult,
        *,
        pdf_directory: Path | None = None,
    ) -> SearchResult:
        """Fetch public research evidence without models or database writes."""
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            links = result.raw_metadata.get("pdf_links") or []
            if not links and result.raw_metadata.get("pdf_link"):
                links = [result.raw_metadata["pdf_link"]]
            if links:
                return await hydrate_pdf(
                    client, result, links, self.last_search, pdf_directory=pdf_directory
                )
            return await self._fetch_detail(
                client, result, download_pdfs=True, pdf_directory=pdf_directory
            )

    async def _fetch_detail(
        self,
        client,
        result: SearchResult,
        *,
        download_pdfs: bool | None = None,
        pdf_directory: Path | None = None,
    ) -> SearchResult:
        if not result.source_url:
            raise SourceParseError("missing report detail URL")
        data = await bounded_download(client, result.source_url, MAX_HTML_BYTES)
        soup = BeautifulSoup(data, "html.parser")
        article = soup.select_one(
            ".ctx-content, .newsContent, .news-content, .report-content, article"
        )
        text = article_text(article)
        links = pdf_links(soup, result.source_url)
        updates = {
            "abstract": text[:1500] if text else None,
            "raw_metadata": {
                **result.raw_metadata,
                "content_kind": "html" if text else "metadata_only",
                "text_scope": "html_article" if text else "metadata_only",
                "pdf_link": links[0] if links else None,
                "pdf_links": links,
            },
        }
        if usable_full_text(text):
            updates.update(full_text=text, abstract_only=bool(links))
        result = result.model_copy(update=updates)
        # Public HTML is often an excerpt; the linked report is the better source.
        should_download = self.download_pdfs if download_pdfs is None else download_pdfs
        if links and should_download:
            result = await hydrate_pdf(
                client, result, links, self.last_search, pdf_directory=pdf_directory
            )
        if not result.abstract and not result.full_text and not links:
            self.last_search.record_error(
                SourceParseError("no research text or public PDF in report page"),
                "article content",
            )
        return result

    def _filter_by_keywords(
        self, results: list[SearchResult], criteria: UserCriteria
    ) -> list[SearchResult]:
        expanded = expand_topics(criteria.topics, criteria.keywords, lang="zh")
        user_terms = [t.lower() for t in expanded]
        all_terms = list(set(user_terms + QUANT_KEYWORDS))

        scored: list[tuple[int, SearchResult]] = []
        for r in results:
            search_text = (
                (r.title or "")
                + " "
                + (r.abstract or "")
                + " "
                + r.raw_metadata.get("industry_name", "")
                + " "
                + r.raw_metadata.get("stock_name", "")
            ).lower()

            if any(ep in search_text for ep in EXCLUDE_PATTERNS):
                continue

            precise_score = sum(
                1 for t in all_terms if t in search_text and t not in AMBIGUOUS_TERMS
            )
            ambig_score = sum(
                1 for t in all_terms if t in search_text and t in AMBIGUOUS_TERMS
            )
            user_precise = (
                sum(
                    1
                    for t in user_terms
                    if t in search_text and t not in AMBIGUOUS_TERMS
                )
                if user_terms
                else 0
            )

            if precise_score == 0 and ambig_score > 0:
                continue

            total_score = precise_score + ambig_score + user_precise * 3

            if total_score > 0:
                scored.append((total_score, r))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored]

    async def _fetch_reports(
        self,
        client: httpx.AsyncClient,
        q_type: int,
        date_from: str,
        date_to: str,
        max_results: int | None,
    ) -> list[SearchResult]:
        results: list[SearchResult] = []
        page_size = (
            min(max_results, FETCH_PAGE_SIZE) if max_results else FETCH_PAGE_SIZE
        )
        page_cap = (
            None
            if max_results is None
            else min(
                (max_results + page_size - 1) // page_size,
                FETCH_PAGES_PER_QTYPE,
            )
        )

        page_no = 1
        seen_codes: set[str] = set()
        seen_pages: set[bytes] = set()
        expected_pages = None
        while True:
            params = {
                "industryCode": "*",
                "pageSize": page_size,
                "industry": "*",
                "rating": "*",
                "ratingChange": "*",
                "beginTime": date_from,
                "endTime": date_to,
                "pageNo": page_no,
                "fields": "",
                "qType": q_type,
                "orgCode": "",
                "rcode": "",
                "_": "1",
            }

            try:
                resp = await client.get(LIST_API, params=params)
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                    raise SourceParseError("invalid JSON report-list schema")
                items = data["data"]
                pages = data.get("TotalPage")
                if not isinstance(pages, int) or isinstance(pages, bool) or pages < 0:
                    raise SourceParseError("invalid JSON pagination")
                if expected_pages is not None and pages != expected_pages:
                    self.last_search.record_error(
                        SourceParseError("pagination total changed during enumeration"),
                        f"qType {q_type} page {page_no}",
                    )
                expected_pages = max(expected_pages or 0, pages)
                if not items and pages > 0:
                    raise SourceParseError(
                        "unexpected empty page before pagination exhaustion"
                    )
                if items and pages < page_no:
                    raise SourceParseError("inconsistent JSON pagination")
            except Exception as error:
                logger.warning(
                    "EastMoney %s",
                    self.last_search.record_error(
                        error, f"qType {q_type} page {page_no}"
                    ),
                )
                break  # Keep earlier pages, then continue with other qTypes.

            self.last_search.fetched += len(items)
            page_fingerprint = sha256(
                json.dumps(items, sort_keys=True).encode()
            ).digest()
            repeated_page = page_fingerprint in seen_pages
            seen_pages.add(page_fingerprint)
            parsed_count = 0
            new_codes = 0
            for item in items:
                try:
                    if not isinstance(item, dict):
                        raise SourceParseError("invalid report item")
                    sr = self._parse_item(item, q_type)
                    if not sr or not sr.title.strip() or not sr.source_url:
                        raise SourceParseError(
                            "report item missing title or identifier"
                        )
                    parsed_count += 1
                    code = sr.source_url
                    if code not in seen_codes:
                        seen_codes.add(code)
                        new_codes += 1
                        results.append(sr)
                except Exception as error:
                    self.last_search.record_error(
                        error, f"qType {q_type} page {page_no} item"
                    )
            if parsed_count or not items:
                self.last_search.successful_requests += 1
            # A parse failure does not prove that pagination is repeating. Keep
            # enumerating the advertised pages, but still stop actual repeats
            # (including wholly malformed pages) and fully parsed duplicate pages.
            if items and (
                repeated_page or (parsed_count == len(items) and not new_codes)
            ):
                self.last_search.record_error(
                    SourceParseError("pagination made no progress"),
                    f"qType {q_type} page {page_no}",
                )
                break
            if max_results is not None and len(results) >= max_results:
                if page_no < expected_pages:
                    self.last_search.record_error(
                        SourceParseError(
                            "pagination stopped at configured result limit"
                        ),
                        f"qType {q_type} page {page_no}",
                    )
                return results[:max_results]
            if not items or page_no >= expected_pages:
                break
            if page_cap is not None and page_no >= page_cap:
                self.last_search.record_error(
                    SourceParseError("pagination stopped at configured page limit"),
                    f"qType {q_type} page {page_no}",
                )
                break

            await asyncio.sleep(self.rate_limit_seconds)
            page_no += 1

        return results

    def _parse_item(self, item: dict, q_type: int) -> Optional[SearchResult]:
        title = item.get("title", "")
        if not title:
            return None

        info_code = item.get("infoCode", "")
        org_name = item.get("orgSName") or item.get("orgName", "")
        researcher = item.get("researcher", "")
        authors = (
            [a.strip() for a in researcher.split(",") if a.strip()]
            if researcher
            else []
        )

        pub_date_str = item.get("publishDate", "")
        pub_date = None
        announced_at = None
        if pub_date_str:
            try:
                pub_date = datetime.strptime(pub_date_str[:10], "%Y-%m-%d")
            except ValueError:
                pass
            try:
                # A calendar date cannot locate an announcement within an
                # intraday window. Require a clock supplied by the source.
                if not re.match(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}", pub_date_str):
                    raise ValueError("publishDate has no announcement time")
                stamp = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
                announced_at = (
                    stamp.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                    if stamp.tzinfo is None
                    else stamp
                ).astimezone(timezone.utc)
            except (ValueError, TypeError):
                pass

        stock_name = item.get("stockName", "")
        stock_code = item.get("stockCode", "")
        industry_name = item.get("indvInduName", "")
        rating = item.get("emRatingName", "")
        report_type_label = REPORT_TYPE_LABELS.get(q_type, "研报")

        # Institution, type, page count and forecasts are metadata, not a research abstract.
        abstract = None

        source_url = DETAIL_BASE.format(info_code=info_code) if info_code else None

        return SearchResult(
            title=title,
            authors=authors,
            abstract=abstract,
            full_text=None,
            abstract_only=True,
            source=SourceType.EASTMONEY,
            source_url=source_url,
            published_date=pub_date,
            announced_at=announced_at,
            raw_metadata={
                "announcement_info": {
                    "source": "publishDate" if announced_at else None,
                    "evidence": pub_date_str or None,
                },
                "info_code": info_code,
                "org_name": org_name,
                "org_code": item.get("orgCode", ""),
                "stock_name": stock_name,
                "stock_code": stock_code,
                "industry_name": industry_name,
                "rating": rating,
                "rating_change": item.get("ratingChange", ""),
                "report_type": q_type,
                "report_type_label": report_type_label,
                "attach_pages": item.get("attachPages"),
                "attach_size": item.get("attachSize"),
                "encode_url": item.get("encodeUrl", ""),
                "market": item.get("market", ""),
            },
        )

    def _resolve_qtypes(self, criteria: UserCriteria) -> list[int]:
        if criteria.date_basis == "announcement":
            return sorted(REPORT_TYPE_LABELS)
        q_types: set[int] = set()
        expanded = expand_topics(criteria.topics, criteria.keywords, lang="zh")

        for term in expanded:
            term_lower = term.lower()
            for key, types in TOPIC_TO_QTYPES.items():
                if key in term_lower or term_lower in key:
                    q_types.update(types)

        if not q_types:
            q_types = {2}

        return sorted(q_types)

    def _resolve_dates(self, criteria: UserCriteria) -> tuple[str, str]:
        # The list API accepts calendar days in the site's own timezone. Exact
        # intraday bounds are applied to publishDate after metadata enumeration.
        if criteria.date_basis == "announcement":
            site_zone = ZoneInfo("Asia/Shanghai")
            end = utc_datetime(criteria.date_to or datetime.now(timezone.utc))
            start = utc_datetime(criteria.date_from or end - timedelta(days=180))
            return (
                start.astimezone(site_zone).strftime("%Y-%m-%d"),
                end.astimezone(site_zone).strftime("%Y-%m-%d"),
            )
        if criteria.date_to:
            date_to = criteria.date_to.strftime("%Y-%m-%d")
        else:
            date_to = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if criteria.date_from:
            date_from = criteria.date_from.strftime("%Y-%m-%d")
        else:
            date_from = (datetime.now(timezone.utc) - timedelta(days=180)).strftime(
                "%Y-%m-%d"
            )

        return date_from, date_to
