from __future__ import annotations

import asyncio
import logging

from reportagent.agents.state import AgentState
from reportagent.models.schemas import SearchResult, SourceType
from reportagent.sources.base import BaseSource
from reportagent.llm.client import LLMClient
from reportagent.utils.hashing import normalize_text

logger = logging.getLogger(__name__)


class CollectionAgent:
    def __init__(self, sources: list[BaseSource], llm_client: LLMClient | None = None):
        self.sources = sources
        self.llm_client = llm_client

    async def run(self, state: AgentState) -> AgentState:
        criteria = state["criteria"]
        state["collection_status"] = "searching"
        state["current_phase"] = "collecting"
        state["messages"].append("Starting collection across sources...")

        enabled_sources = [
            s for s in self.sources
            if s.source_type in criteria.sources and s.is_available()
        ]

        if not enabled_sources:
            state["collection_status"] = "done"
            state["collection_errors"].append("No enabled/available sources found")
            state["messages"].append("No sources available for search")
            return state

        source_names = [s.source_type.value for s in enabled_sources]
        state["messages"].append(f"Searching {len(enabled_sources)} sources: {', '.join(source_names)}")

        tasks = [source.search(criteria) for source in enabled_sources]
        try:
            results_per_source = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=180,
            )
        except asyncio.TimeoutError:
            state["collection_errors"].append("Collection timed out after 180s")
            state["messages"].append("Collection timed out")
            results_per_source = []

        all_results: list[SearchResult] = []
        for source, result in zip(enabled_sources, results_per_source):
            if isinstance(result, Exception):
                err = f"{source.source_type.value}: {result}"
                state["collection_errors"].append(err)
                logger.warning("Source %s failed: %s", source.source_type.value, result)
                continue
            all_results.extend(result)
            state["messages"].append(
                f"Found {len(result)} results from {source.source_type.value}"
            )

        deduped = self._deduplicate(all_results)
        state["raw_results"] = deduped
        state["collection_status"] = "done"
        state["messages"].append(
            f"Collection complete: {len(deduped)} unique results "
            f"(from {len(all_results)} total)"
        )
        return state

    def _deduplicate(self, results: list[SearchResult]) -> list[SearchResult]:
        seen: dict[str, SearchResult] = {}
        for r in results:
            key = normalize_text(r.title)

            if r.doi:
                doi_key = f"doi:{r.doi}"
                if doi_key in seen:
                    self._merge_into(seen[doi_key], r)
                    continue
                seen[doi_key] = r

            if key in seen:
                self._merge_into(seen[key], r)
                continue

            seen[key] = r

        return list(seen.values())

    def _merge_into(self, existing: SearchResult, new: SearchResult) -> None:
        if new.full_text and not existing.full_text:
            existing.full_text = new.full_text
            existing.abstract_only = False
        if new.abstract and not existing.abstract:
            existing.abstract = new.abstract
        if new.doi and not existing.doi:
            existing.doi = new.doi
