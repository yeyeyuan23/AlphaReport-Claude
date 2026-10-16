import json
import subprocess
import sys
import unittest
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.db.repository import ReportRepository
from reportagent.models.database import Base, Report
from reportagent.models.schemas import ClassifiedReport, ClassificationResult, SearchResult, SourceType
from reportagent.services import daily_push as daily
from reportagent.services.paper_scoring import evidence_fingerprint


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "configs").mkdir()
        (root / "configs/push_directions.yaml").write_text("push_settings: {}")
        async def reviews(ids, **kwargs):
            from reportagent.services.report_quality import review_fingerprint
            results = {}
            with self.factory() as session:
                for rid in ids:
                    row = session.get(Report, rid)
                    evaluation = {"tier": "browse", "label": "值得浏览", "status": "reviewed", "findings": [],
                                  "limitations": ["测试范围"], "source_category": "academic", "model": "fixture-model",
                                  "fingerprint": review_fingerprint(vars(row), "fixture-model")}
                    row.evaluation_json = json.dumps(evaluation)
                    results[rid] = evaluation
                session.commit()
            return results
        self.patches = [patch.object(daily, "init_db"), patch.object(daily, "get_session_factory", return_value=self.factory),
                        patch.object(daily, "PROJECT_ROOT", root), patch.object(daily, "review_report_ids", side_effect=reviews),
                        patch.object(daily.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", ""))]
        for item in self.patches:
            item.start()
        with self.factory() as session:
            rows = [Report(title=f"Paper {i}", source="arxiv", content_hash=f"hash-{i}",
                           abstract="We study portfolio allocation and compare rolling trading policies with transaction costs.",
                           published_date=datetime.now(timezone.utc), is_selected=True, selection_relevance=8)
                    for i in range(1, 13)]
            for row in rows:
                row.selection_provenance_json = json.dumps({"fingerprint": daily.scoring_fingerprint(),
                    "status": "scored", "evidence_fingerprint": evidence_fingerprint(vars(row))})
            session.add_all(rows)
            session.commit()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.engine.dispose()
        self.temp.cleanup()

    def run_daily(self, send):
        with patch.object(daily, "sync_public_url"), patch.object(
            daily.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
        ), patch.object(daily, "analyze_with_claude", side_effect=lambda papers, label: daily.fallback_briefing(papers, label)), patch.object(
            daily, "send_feishu_card", side_effect=send
        ), patch.object(daily, "deep_analyze_selected"):
            daily.run_daily_push(daily.DailyPushOptions(no_collect=True, receive_ids="A,B"))

    def test_each_daily_run_sends_the_period_to_all_requested_recipients(self):
        deliveries = []
        def first(rid, briefing, papers, *args):
            deliveries.append((rid, len(papers)))
            return rid == "A"
        with self.assertRaisesRegex(RuntimeError, "投递"):
            self.run_daily(first)
        self.assertEqual(deliveries, [("A", 12), ("B", 12)])
        deliveries.clear()
        def second(rid, briefing, papers, *args):
            deliveries.append((rid, len(papers)))
            return True
        self.run_daily(second)
        self.assertEqual(deliveries, [("A", 12), ("B", 12)])
        deliveries.clear()
        self.run_daily(second)
        self.assertEqual(deliveries, [("A", 12), ("B", 12)])

    def test_fallback_includes_all_paper_links(self):
        papers = [{"id": i, "title": f"Paper {i}", "ui_url": f"https://fixture.test/library/{i}"} for i in range(1, 21)]
        brief = daily.fallback_briefing(papers, "fixture")
        self.assertEqual(brief.count("[📄 查看]"), 20)

    def test_optional_url_sync_failure_does_not_stop_daily_delivery(self):
        deliveries = []
        failures = [PermissionError("read-only settings"),
                    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid log encoding")]
        for index, error in enumerate(failures):
            with self.subTest(error=type(error).__name__), patch.object(
                daily, "sync_public_url", side_effect=error
            ), patch.object(
                daily, "analyze_with_claude", side_effect=lambda papers, label: daily.fallback_briefing(papers, label)
            ), patch.object(
                daily, "send_feishu_card", side_effect=lambda recipient, *args: deliveries.append(recipient) or True
            ), patch.object(daily, "deep_analyze_selected"):
                daily.run_daily_push(daily.DailyPushOptions(no_collect=True, receive_ids=f"A{index}"))
        self.assertEqual(deliveries, ["A0", "A1"])

    def test_text_fallback_never_marks_truncated_content_as_delivered(self):
        import lark_oapi
        from unittest.mock import MagicMock
        client = MagicMock()
        client.im.v1.message.create.return_value.success.return_value = False
        sent = []
        papers = [{"id": i, "title": "Long title " * 20, "ui_url": f"https://fixture.test/library/{i}"} for i in range(1, 21)]
        briefing = daily.fallback_briefing(papers, "fixture")
        def send(rid, text, receive_type):
            sent.append(text)
            return True
        with patch.object(lark_oapi.Client, "builder") as builder, patch.object(daily, "send_feishu_text", side_effect=send):
            builder.return_value.app_id.return_value.app_secret.return_value.build.return_value = client
            self.assertTrue(daily.send_feishu_card("A", briefing, papers))
        self.assertIn("https://fixture.test/library/20", "".join(sent))


class RepositoryRecoveryTests(unittest.TestCase):
    def test_unjudged_retry_uses_publication_window_instead_of_creation_date(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        now = datetime.now(timezone.utc)
        with sessionmaker(bind=engine)() as session:
            session.add_all([
                Report(title="retry", source="arxiv", content_hash="retry", created_at=now - timedelta(days=2), published_date=now - timedelta(days=1)),
                Report(title="expired", source="arxiv", content_hash="expired", created_at=now, published_date=now - timedelta(days=10)),
            ])
            session.commit()
            reports = ReportRepository(session).get_unjudged_reports(published_since=now - timedelta(days=3))
            self.assertEqual([r.title for r in reports], ["retry"])
        engine.dispose()

    def test_next_record_survives_failed_transaction(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        with sessionmaker(bind=engine)() as session:
            repo = ReportRepository(session)
            original = repo.upsert_report
            def upsert(report):
                if report.search_result.title == "bad":
                    session.add(Report(title=None, source="arxiv", content_hash="bad"))
                    session.commit()
                return original(report)
            papers = [ClassifiedReport(search_result=SearchResult(title=title, source=SourceType.ARXIV), classification=ClassificationResult()) for title in ["bad", "good"]]
            with patch.object(repo, "upsert_report", side_effect=upsert):
                result = repo.batch_upsert(papers)
            self.assertEqual(result.newly_added, 1)
            self.assertEqual(len(result.errors), 1)
            self.assertEqual(session.query(Report).one().title, "good")
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
