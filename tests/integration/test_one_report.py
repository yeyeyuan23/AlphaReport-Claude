import unittest
import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from reportagent.models.database import Report
from reportagent.models.schemas import AnalysisResult, SearchResult, SourceType
from reportagent.services.one_report import (
    collect_paper,
    normalize_arxiv_id,
    validate_analysis,
    push_artifact,
    _send_to_chat,
    store_paper,
    prepare_report,
)


from tests.support.database import memory_database
from tests.support.report_fixtures import TEXT, complete_analysis


class OneReportTests(unittest.TestCase):
    def test_real_send_boundary_checks_api_failure_and_message_id(self):
        client = MagicMock()
        response = client.im.v1.message.create.return_value
        response.success.return_value = False
        response.code = 999
        with patch(
            "reportagent.services.feishu_transport.build_client", return_value=client
        ):
            with self.assertRaises(RuntimeError):
                _send_to_chat("oc_private", "摘要", "test-request")
            response.success.return_value = True
            response.data.message_id = "om_real"
            self.assertEqual(
                _send_to_chat("oc_private", "摘要", "test-request"), "om_real"
            )
        request = client.im.v1.message.create.call_args.args[0]
        self.assertEqual(request.receive_id_type, "chat_id")
        self.assertEqual(request.request_body.receive_id, "oc_private")
        self.assertEqual(request.request_body.uuid, "test-request")

    def test_id_accepts_official_link_and_rejects_arbitrary_urls(self):
        self.assertEqual(
            normalize_arxiv_id("https://arxiv.org/abs/2005.13665v3"), "2005.13665v3"
        )
        for value in ["../../secret", "https://evil.test/2005.13665", "2005.13665;pwd"]:
            with self.assertRaises(ValueError):
                normalize_arxiv_id(value)

    def test_partial_analysis_cannot_be_pushed(self):
        artifact = {"analysis": AnalysisResult(summary="Looks successful").model_dump()}
        with patch("reportagent.services.one_report._send_to_chat") as send:
            with self.assertRaises(ValueError):
                push_artifact(artifact, "oc_test")
            send.assert_not_called()

    def test_empty_methodology_is_not_success(self):
        data = complete_analysis()
        data["methodology"] = {}
        with self.assertRaises(ValueError):
            validate_analysis(data)

    def test_push_uses_explicit_recipient_and_keeps_receipt(self):
        artifact = {
            "analysis": complete_analysis(),
            "brief": "研报摘要",
            "deliveries": [],
        }
        with patch(
            "reportagent.services.one_report._send_to_chat", return_value="om_test"
        ) as send:
            receipt = push_artifact(artifact, "oc_private")
            self.assertEqual(send.call_args.args[:2], ("oc_private", "研报摘要"))
            self.assertEqual(receipt["message_id"], "om_test")
            self.assertEqual(receipt["chat_id"], "oc_private")
            push_artifact(artifact, "oc_private")
            self.assertEqual(send.call_count, 1)

    def test_failed_send_does_not_record_delivery(self):
        artifact = {"analysis": complete_analysis(), "brief": "摘要", "deliveries": []}
        with patch(
            "reportagent.services.one_report._send_to_chat",
            side_effect=RuntimeError("rejected"),
        ):
            with self.assertRaises(RuntimeError):
                push_artifact(artifact, "oc_private")
        self.assertEqual(artifact["deliveries"], [])


class CollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_analysis_fingerprint_changes_when_prompt_changes(self):
        from reportagent.services import one_report

        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / "analysis.yaml"
            prompt.write_text("old analysis rules")
            with patch.object(one_report, "skill_resource", return_value=prompt):
                before = one_report.analysis_fingerprint()
                prompt.write_text("new evidence rules")
                self.assertNotEqual(before, one_report.analysis_fingerprint())

    async def test_cached_revision_restores_matching_database_contents(self):
        factory = self.enterContext(memory_database())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"pdf-test")
            paper = SearchResult(
                title="Same title",
                authors=["Author"],
                source=SourceType.ARXIV,
                arxiv_id="2005.13665v1",
                full_text=TEXT + "version one",
                pdf_path=str(pdf),
                source_url="https://arxiv.org/abs/2005.13665v1",
            )
            result = AnalysisResult.model_validate(complete_analysis())
            artifact_path = root / "data/one_reports/2005.13665v1.json"
            artifact_path.parent.mkdir(parents=True)
            from reportagent.services.one_report import analysis_fingerprint

            artifact_path.write_text(
                json.dumps(
                    {
                        "analysis": result.model_dump(),
                        "analysis_fingerprint": analysis_fingerprint(),
                        "evaluation": {"status": "reviewed", "tier": "browse"},
                        "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
                        "deliveries": [{"message_id": "saved"}],
                    }
                )
            )
            with (
                patch("reportagent.services.one_report.init_db"),
                patch(
                    "reportagent.services.one_report.get_session_factory",
                    return_value=factory,
                ),
                patch("reportagent.services.one_report.PROJECT_ROOT", root),
                patch(
                    "reportagent.services.one_report.collect_paper",
                    new=AsyncMock(return_value=paper),
                ),
                patch("reportagent.services.one_report.LLMClient") as llm,
            ):
                store_paper(paper, result)
                from reportagent.services.one_report import _stored_review_input
                from reportagent.services.report_quality import (
                    SCHEMA_VERSION,
                    review_fingerprint,
                )
                from reportagent.utils.config import get_llm_model

                cached = json.loads(artifact_path.read_text())
                cached["evaluation"].update(
                    schema_version=SCHEMA_VERSION,
                    model=get_llm_model(),
                    text_complete=True,
                )
                cached["evaluation"]["fingerprint"] = review_fingerprint(
                    _stored_review_input(1), get_llm_model()
                )
                artifact_path.write_text(json.dumps(cached))
                store_paper(
                    paper.model_copy(
                        update={
                            "arxiv_id": "2005.13665v2",
                            "full_text": "version two",
                            "title": "New title",
                        }
                    ),
                    result,
                )
                with factory() as session:
                    row = session.query(Report).one()
                    row.selection_relevance, row.selection_provenance_json = (
                        9,
                        '{"new_revision":true}',
                    )
                    session.commit()
                _, artifact = await prepare_report("2005.13665v1")
                llm.assert_not_called()
                self.assertEqual(artifact["deliveries"][0]["message_id"], "saved")
                with factory() as session:
                    stored = session.get(Report, artifact["report_id"])
                    self.assertEqual(stored.arxiv_id, "2005.13665v1")
                    self.assertEqual(stored.full_text, TEXT + "version one")
                    self.assertEqual(stored.title, "Same title")
                    self.assertIsNone(stored.selection_relevance)
                    self.assertIsNone(stored.selection_provenance_json)
                    self.assertEqual(
                        json.loads(stored.analysis_json), result.model_dump()
                    )

    async def test_pending_quality_retries_without_repeating_valid_analysis(self):
        from reportagent.services import one_report, report_quality

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"pdf")
            paper = SearchResult(
                title="Paper",
                source=SourceType.ARXIV,
                arxiv_id="2005.13665v1",
                full_text=TEXT,
                pdf_path=str(pdf),
            )
            path = root / "data/one_reports/2005.13665v1.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "analysis": complete_analysis(),
                        "evaluation": {
                            "status": "needs_verification",
                            "tier": "pending",
                        },
                        "analysis_fingerprint": one_report.analysis_fingerprint(),
                        "pdf_sha256": hashlib.sha256(b"pdf").hexdigest(),
                        "deliveries": [],
                        "phase_input_chars": {"methodology": len(TEXT)},
                    }
                )
            )
            llm = MagicMock(model="fixture", thinking="disabled", calls=[])
            llm._client.close = AsyncMock()
            evaluation = report_quality.pending_review({}, "Retry still pending")
            with (
                patch.object(
                    one_report,
                    "_stored_review_input",
                    return_value={"title": "Paper", "full_text": TEXT},
                ),
                patch.object(one_report, "PROJECT_ROOT", root),
                patch.object(one_report, "store_paper", return_value=1),
                patch.object(
                    one_report, "collect_paper", new=AsyncMock(return_value=paper)
                ),
                patch.object(one_report, "LLMClient", return_value=llm),
                patch.object(one_report, "AnalysisAgent") as agent,
                patch.object(
                    report_quality,
                    "evaluate_paper",
                    new=AsyncMock(return_value=evaluation),
                ) as review,
            ):
                _, artifact = await prepare_report("2005.13665v1")
            review.assert_awaited_once()
            agent.assert_not_called()
            self.assertEqual(artifact["evaluation"], evaluation)

    async def test_explicit_revision_is_preserved_with_unversioned_metadata(self):
        response = MagicMock()
        response.text = """<meta name="citation_title" content="Paper">
        <meta name="citation_author" content="Author">
        <meta name="citation_arxiv_id" content="2005.13665">
        <blockquote class="abstract">Abstract: sample</blockquote>arXiv:2005.13665v1"""
        client = AsyncMock()
        client.get.return_value = response
        manager = MagicMock()
        manager.__aenter__ = AsyncMock(return_value=client)
        manager.__aexit__ = AsyncMock()

        async def fetch(client, paper, url):
            self.assertEqual(url, "https://arxiv.org/pdf/2005.13665v1")
            return paper.model_copy(
                update={"full_text": "text " * 500, "abstract_only": False}
            )

        absent_db = MagicMock()
        absent_db.exists.return_value = False
        with (
            patch(
                "reportagent.services.one_report.get_db_path", return_value=absent_db
            ),
            patch(
                "reportagent.services.one_report.httpx.AsyncClient",
                return_value=manager,
            ),
            patch(
                "reportagent.services.one_report.ArxivSource._fetch_one_pdf",
                side_effect=fetch,
            ),
        ):
            paper = await collect_paper("2005.13665v1")
        self.assertEqual(paper.arxiv_id, "2005.13665v1")


if __name__ == "__main__":
    unittest.main()


class SharedEvaluationSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_single_evaluation_uses_exact_persisted_snapshot(self):
        from reportagent.services import one_report, report_quality

        factory = self.enterContext(memory_database(expire_on_commit=False))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"pdf")
            paper = SearchResult(
                title="Portfolio methods",
                abstract="We study portfolio methods and costs.",
                full_text=TEXT,
                abstract_only=False,
                source=SourceType.ARXIV,
                arxiv_id="2005.13665v1",
                pdf_path=str(pdf),
            )
            llm = MagicMock(model="fixture", thinking="disabled", calls=[])
            llm._client.close = AsyncMock()
            agent = MagicMock(_config={"analysis": {"phases": {}}})
            agent.analyze_report_directly = AsyncMock(
                return_value=AnalysisResult.model_validate(complete_analysis())
            )

            async def evaluate(snapshot, model, **kwargs):
                with factory() as session:
                    row = session.query(Report).one()
                    expected = report_quality.review_fingerprint(vars(row), model.model)
                assert (
                    report_quality.review_fingerprint(snapshot, model.model) == expected
                )
                return {
                    "status": "reviewed",
                    "tier": "browse",
                    "fingerprint": expected,
                }

            with (
                patch.object(one_report, "PROJECT_ROOT", root),
                patch.object(one_report, "init_db"),
                patch.object(one_report, "get_session_factory", return_value=factory),
                patch.object(
                    one_report, "collect_paper", new=AsyncMock(return_value=paper)
                ),
                patch.object(one_report, "LLMClient", return_value=llm),
                patch.object(one_report, "AnalysisAgent", return_value=agent),
                patch.object(report_quality, "evaluate_paper", side_effect=evaluate),
                patch.object(one_report, "make_brief", return_value="brief"),
            ):
                await one_report.prepare_report(paper.arxiv_id)

    async def test_storing_same_paper_without_new_result_preserves_success(self):
        from reportagent.services import one_report

        factory = self.enterContext(memory_database())
        paper = SearchResult(
            title="Same paper",
            source=SourceType.ARXIV,
            arxiv_id="2005.13665v1",
            full_text=TEXT,
        )
        result = AnalysisResult.model_validate(complete_analysis())
        with (
            patch.object(one_report, "init_db"),
            patch.object(one_report, "get_session_factory", return_value=factory),
        ):
            rid = one_report.store_paper(
                paper, result, {"status": "reviewed", "tier": "browse"}
            )
            one_report.store_paper(paper)
        with factory() as session:
            row = session.get(Report, rid)
            assert row.analysis_json == result.model_dump_json()
            assert json.loads(row.evaluation_json)["status"] == "reviewed"
