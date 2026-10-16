"""Offline checks of acceptance coverage and no-delivery/date boundaries."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.models.database import Base, Report
from tests.acceptance import workflows


def test_full_library_acceptance_rescores_old_and_already_scored_rows(tmp_path, monkeypatch):
    from reportagent.llm import client
    from reportagent.services import paper_scoring
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as session:
            session.add_all([Report(title=f"paper-{i}", content_hash=str(i), source="arxiv",
                                    abstract="Portfolio model", selection_relevance=9,
                                    is_selected=True, published_date=datetime(2000, 1, 1))
                             for i in range(207)])
            session.commit()
        for module in (workflows, paper_scoring):
            monkeypatch.setattr(module, "get_session_factory", lambda: factory)
        monkeypatch.setattr(paper_scoring, "init_db", lambda: None)
        llm = MagicMock(calls=[])
        llm._client.close = AsyncMock()
        monkeypatch.setattr(client, "LLMClient", lambda: llm)
        observed = []

        async def assess(model, papers, **kwargs):
            observed.extend(p.id for p in papers)
            return [{"_paper": p, "relevance": 2, "reason": "Only weak abstract overlap",
                     "best_direction": None} for p in papers], []

        monkeypatch.setattr(paper_scoring, "score_papers_batch", assess)
        result = asyncio.run(workflows.score(SimpleNamespace(run_dir=tmp_path, label="score", review=False)))
        assert len(observed) == len(set(observed)) == 207
        assert result["all_scored"]
        assert result["stats"]["rejected"] == 207
        with factory() as session:
            assert not session.query(Report).filter(Report.is_selected.is_(True)).count()
        llm._client.close.assert_awaited_once()
    finally:
        engine.dispose()


def test_weekly_preview_uses_publication_boundaries_and_cannot_deliver(tmp_path, monkeypatch):
    from reportagent.services import weekly_report as weekly
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    start = datetime(2026, 10, 5, tzinfo=timezone.utc)
    end = start + timedelta(days=7) - timedelta(microseconds=1)
    try:
        with factory() as session:
            for i, stamp in enumerate((start - timedelta(microseconds=1), start,
                                       end, end + timedelta(microseconds=1))):
                session.add(Report(title=f"paper-{i}", content_hash=str(i), source="arxiv",
                                   published_date=stamp, created_at=datetime(2026, 10, 16)))
            session.commit()
        monkeypatch.setattr(weekly, "get_session_factory", lambda: factory)
        monkeypatch.setattr(weekly, "init_db", lambda: None)
        monkeypatch.setattr(weekly, "get_previous_week_range", lambda *args: (
            start, end, "2026-10-05", "2026-10-11"))
        send = MagicMock(side_effect=AssertionError("Acceptance must not send"))
        monkeypatch.setattr(weekly, "send_feishu_card", send)
        result = workflows.weekly(SimpleNamespace(run_dir=tmp_path, label="weekly", llm_weekly=False))
        assert result["paper_ids"] == [3, 2]
        assert result["sent"] is False
        assert result["fallback"] is True
        assert (tmp_path / "weekly.md").exists()
        send.assert_not_called()
    finally:
        engine.dispose()
