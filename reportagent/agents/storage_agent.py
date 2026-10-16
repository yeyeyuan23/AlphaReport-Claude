from __future__ import annotations

import logging

from reportagent.agents.state import AgentState
from reportagent.db.engine import get_session_factory
from reportagent.db.repository import ReportRepository

logger = logging.getLogger(__name__)


class StorageAgent:
    async def run(self, state: AgentState) -> AgentState:
        state["storage_status"] = "storing"
        state["current_phase"] = "storing"
        state["messages"].append(
            f"Storing {len(state['classified_reports'])} reports..."
        )

        factory = get_session_factory()
        session = factory()
        try:
            repo = ReportRepository(session)
            result = repo.batch_upsert(state["classified_reports"])
            state["storage_result"] = result
            state["storage_status"] = "done"
            state["messages"].append(
                f"Storage complete: {result.newly_added} new, "
                f"{result.updated} updated, "
                f"{result.duplicate_skipped} duplicates skipped"
            )
            if result.errors:
                for err in result.errors:
                    logger.warning("Storage error: %s", err)

            self._index_into_vector_store(state["classified_reports"])

        except Exception as e:
            logger.error("Storage agent error: %s", e)
            state["storage_status"] = "error"
            state["messages"].append(f"Storage error: {e}")
        finally:
            session.close()

        state["current_phase"] = "complete"
        return state

    @staticmethod
    def _index_into_vector_store(reports) -> None:
        """Index newly-added reports into the vector store for semantic search."""
        try:
            from reportagent.vector_store.store import get_vector_store
            from reportagent.utils.hashing import content_hash

            store = get_vector_store()
            factory = get_session_factory()
            session = factory()
            try:
                from reportagent.models.database import Report
                from reportagent.classifiers.quant_filter import compute_quant_score
                indexed = 0
                for cr in reports:
                    sr = cr.search_result
                    cl = cr.classification
                    text = sr.full_text or sr.abstract or ""
                    if not text.strip():
                        continue
                    c_hash = content_hash(sr.title, sr.authors)
                    db_report = session.query(Report).filter(Report.content_hash == c_hash).first()
                    if not db_report:
                        continue
                    topics = ",".join(t.value for t in cl.topics)
                    markets = ",".join(m.value for m in cl.markets)
                    quant_score, _ = compute_quant_score(sr.title, sr.abstract or "")
                    chunks = store.index_report(
                        report_id=db_report.id,
                        title=sr.title,
                        text=text,
                        source=sr.source.value,
                        topics=topics,
                        markets=markets,
                        quant_score=quant_score,
                    )
                    indexed += chunks
                if indexed:
                    logger.info("Vector store: indexed %d chunks from %d reports", indexed, len(reports))
            finally:
                session.close()
        except Exception as e:
            logger.warning("Vector store indexing skipped: %s", e)
