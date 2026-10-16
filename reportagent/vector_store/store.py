from __future__ import annotations
from typing import Optional

import json
import logging
from functools import lru_cache
from pathlib import Path

import chromadb
from chromadb.config import Settings as ChromaSettings

from reportagent.utils.config import PROJECT_ROOT, get_config
from reportagent.vector_store.embeddings import get_embedding_client

logger = logging.getLogger(__name__)

COLLECTION_NAME = "report_chunks"


def _chunk_report(title: str, text: str, chunk_size: int = 1000, overlap: int = 150) -> list[dict]:
    """Split report text into overlapping chunks, trying to break at section boundaries."""
    if not text or not text.strip():
        return []

    chunks: list[dict] = []
    paragraphs = text.split("\n\n")

    current = ""
    current_section = ""

    for para in paragraphs:
        if para.startswith("#") or para.startswith("## "):
            if len(current) > 100:
                chunks.append({"section": current_section, "text": current.strip()})
            current_section = para.strip("# ").strip()
            current = para + "\n\n"
        elif len(current) + len(para) > chunk_size:
            chunks.append({"section": current_section, "text": current.strip()})
            current = current[-overlap:] if overlap and len(current) > overlap else ""
            current += para + "\n\n"
        else:
            current += para + "\n\n"

    if len(current) > 50:
        chunks.append({"section": current_section, "text": current.strip()})

    # If no chunks, use title as single chunk
    if not chunks:
        chunks.append({"section": "", "text": title})

    return chunks


class VectorStore:
    """ChromaDB-backed semantic search for report library."""

    def __init__(self, persist_dir  : Optional[str] = None):
        if persist_dir is None:
            persist_dir = str(
                PROJECT_ROOT / get_config("vector_store", "persist_dir", default="data/chroma_db")
            )

        Path(persist_dir).mkdir(parents=True, exist_ok=True)

        self._client = chromadb.PersistentClient(
            path=persist_dir,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self._embed = get_embedding_client()
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("VectorStore ready: %d chunks in collection", self._collection.count())

    # -- index ----------------------------------------------------------

    def index_report(
        self,
        report_id: int,
        title: str,
        text: str,
        source: str = "",
        topics: str = "",
        markets: str = "",
        quant_score: float = 0.0,
    ) -> int:
        """Chunk and index a single report. Returns number of chunks indexed."""
        self.remove_report(report_id)

        chunks = _chunk_report(title, text)
        if not chunks:
            return 0

        ids = [f"r{report_id}_c{i}" for i in range(len(chunks))]
        documents = [c["text"] for c in chunks]
        metadatas = [
            {
                "report_id": report_id,
                "title": title[:500],
                "source": source,
                "topics": topics[:200],
                "markets": markets,
                "section": c["section"][:200],
                "chunk_index": i,
                "quant_score": quant_score,
            }
            for i, c in enumerate(chunks)
        ]

        embeddings = self._embed.embed(documents)
        self._collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )
        logger.debug("Indexed report %d: %d chunks", report_id, len(chunks))
        return len(chunks)

    def remove_report(self, report_id: int):
        """Remove all chunks belonging to a report."""
        existing = self._collection.get(
            where={"report_id": report_id},
        )
        if existing["ids"]:
            self._collection.delete(ids=existing["ids"])

    # -- search ---------------------------------------------------------

    def search(
        self,
        query: str,
        limit: int = 10,
        source  : Optional[str] = None,
        topic  : Optional[str] = None,
        market  : Optional[str] = None,
        min_quant_score: float = 0.01,
    ) -> list[dict]:
        """Semantic search across report chunks. Returns deduped results grouped by report.

        min_quant_score: minimum quant relevance score. Default 0.01 is very lenient
        (only filters papers with near-zero quant signals). Set 0.02-0.03 for stricter.
        """
        where: dict = {}
        if source:
            where["source"] = source
        if market:
            where["markets"] = {"$contains": market}

        # ChromaDB doesn't support range filters on metadata natively
        # in 'where' for float values. Apply post-query filtering.
        has_quant_filter = min_quant_score > 0

        query_embedding = self._embed.embed([query])[0]

        results = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=min(limit * 3, 50),
            where=where if where else None,
        )

        if not results["ids"] or not results["ids"][0]:
            return []

        # Deduplicate by report_id, keep highest-score chunk per report
        seen: dict[int, dict] = {}
        for i, chunk_id in enumerate(results["ids"][0]):
            meta = results["metadatas"][0][i]
            rid = meta.get("report_id")
            if rid is None:
                continue

            # Apply quant score filter post-query
            if has_quant_filter and float(meta.get("quant_score", 0)) < min_quant_score:
                continue

            score = 1.0 - results["distances"][0][i] if results.get("distances") else 0.0

            if rid not in seen or score > seen[rid].get("score", -1):
                seen[rid] = {
                    "report_id": rid,
                    "title": meta.get("title", ""),
                    "source": meta.get("source", ""),
                    "topics": meta.get("topics", ""),
                    "markets": meta.get("markets", ""),
                    "match_text": results["documents"][0][i][:500],
                    "score": round(score, 4),
                    "quant_score": float(meta.get("quant_score", 0)),
                }

        # Sort and limit
        sorted_results = sorted(seen.values(), key=lambda x: x["score"], reverse=True)
        return sorted_results[:limit]

    def count(self) -> int:
        return self._collection.count()


@lru_cache(maxsize=1)
def get_vector_store() -> VectorStore:
    return VectorStore()
