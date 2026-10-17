"""Batch selection and persistence against a real isolated SQLite library."""

import json
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

from reportagent.models.database import Report
from reportagent.models.schemas import AnalysisResult, Assessment, Methodology
from reportagent.services import batch_analysis
from tests.support.database import memory_database


def complete_analysis():
    return AnalysisResult(
        research_question="研究问题",
        core_contribution="核心贡献",
        summary="研究总结",
        methodology=Methodology(analysis_points=[{"analysis": "原文给出的方法"}]),
        assessment=Assessment(strengths=["摘要说明的优势"], weaknesses=["细节未报告"]),
    )


class BatchAnalysisTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.factory = self.enterContext(memory_database())
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.llm = MagicMock(calls=[])
        self.llm._client.close = AsyncMock()
        self.stack.enter_context(patch.object(batch_analysis, "init_db"))
        self.stack.enter_context(
            patch.object(
                batch_analysis, "get_session_factory", return_value=self.factory
            )
        )
        self.stack.enter_context(
            patch.object(batch_analysis, "LLMClient", return_value=self.llm)
        )
        self.agent = self.stack.enter_context(
            patch.object(batch_analysis, "AnalysisAgent")
        ).return_value
        self.agent.analyze_report_directly = AsyncMock(side_effect=self.analyze)
        self.stack.enter_context(
            patch.object(batch_analysis.asyncio, "sleep", new=AsyncMock())
        )

    async def analyze(self, **kwargs):
        self.llm.calls.append({"complete": True, "model": "fixture"})
        return complete_analysis()

    def add_report(
        self,
        title,
        *,
        topics="factor_model",
        abstract="We evaluate portfolio optimization with transaction costs using out-of-sample data.",
        full_text=None,
        cached=None,
        has_full_text=False,
    ):
        with self.factory() as session:
            row = Report(
                title=title,
                source="arxiv",
                content_hash=title,
                topics=topics,
                abstract=abstract,
                full_text=full_text,
                analysis_json=cached,
                has_full_text=has_full_text,
            )
            session.add(row)
            session.commit()
            return row.id

    def analysis(self, report_id):
        with self.factory() as session:
            value = session.get(Report, report_id).analysis_json
            return json.loads(value) if value else None

    async def test_topic_matches_complete_tokens_across_more_than_200_rows(self):
        first = self.add_report("old exact", topics="factor_model")
        for i in range(205):
            self.add_report(f"filler {i}", topics="factor_model_extra")
        second = self.add_report("multi topic", topics="ai_ml, factor_model ,other")
        for topic in ("prefactor_model", "factorXmodel", "", None):
            self.add_report(f"other {topic}", topics=topic)
        result = await batch_analysis.run_analysis(
            batch_analysis.AnalysisOptions(topic="factor_model")
        )
        self.assertEqual(result["selected"], 2)
        self.assertEqual(set(result["report_ids"]), {first, second})
        self.assertEqual(result["analyzed"], 2)
        self.assertIsNotNone(self.analysis(first))

    async def test_incomplete_default_analysis_is_failure_and_not_saved(self):
        report_id = self.add_report("incomplete")
        self.agent.analyze_report_directly.side_effect = None
        self.agent.analyze_report_directly.return_value = AnalysisResult(
            summary="Only summary"
        )
        result = await batch_analysis.run_analysis(
            batch_analysis.AnalysisOptions(all=True)
        )
        self.assertEqual(result["failed_ids"], [report_id])
        self.assertEqual(result["analyzed"], 0)
        self.assertIsNone(self.analysis(report_id))
        self.llm._client.close.assert_awaited_once()

    async def test_metadata_or_title_only_material_never_calls_model(self):
        ids = [
            self.add_report("Metadata", abstract="机构：某证券；类型：宏观；页数：10"),
            self.add_report("Same title", abstract="Same title"),
            self.add_report("Missing", abstract="未提供摘要"),
            self.add_report(
                "Fake full text",
                full_text="机构：某证券；类型：宏观；页数：10",
                has_full_text=True,
            ),
        ]
        result = await batch_analysis.run_analysis(
            batch_analysis.AnalysisOptions(all=True)
        )
        self.assertEqual(set(result["skipped_ids"]), set(ids))
        self.agent.analyze_report_directly.assert_not_awaited()
        self.assertTrue(all(self.analysis(rid) is None for rid in ids))

    async def test_quick_summary_does_not_require_methodology_or_assessment(self):
        report_id = self.add_report("quick complete")

        async def analyze(**kwargs):
            self.llm.calls.append({"complete": True})
            return AnalysisResult(
                research_question="问题",
                core_contribution="贡献",
                summary="简读总结",
                depth="quick",
            )

        self.agent.analyze_report_directly.side_effect = analyze
        result = await batch_analysis.run_analysis(
            batch_analysis.AnalysisOptions(all=True, depth="quick")
        )
        self.assertEqual(result["analyzed_ids"], [report_id])
        self.assertEqual(result["failed_ids"], [])
        saved = self.analysis(report_id)
        self.assertEqual(saved["summary"], "简读总结")
        self.assertIsNone(saved["methodology"])
        self.assertIsNone(saved["assessment"])

    async def test_quick_missing_summary_is_failure_and_not_saved(self):
        report_id = self.add_report("quick incomplete")

        async def analyze(**kwargs):
            self.llm.calls.append({"complete": True})
            return AnalysisResult(
                research_question="问题",
                core_contribution="贡献",
                summary="   ",
                depth="quick",
            )

        self.agent.analyze_report_directly.side_effect = analyze
        result = await batch_analysis.run_analysis(
            batch_analysis.AnalysisOptions(all=True, depth="quick")
        )
        self.assertEqual(result["failed_ids"], [report_id])
        self.assertEqual(result["analyzed"], 0)
        self.assertIsNone(self.analysis(report_id))

    async def test_failed_or_truncated_call_does_not_poison_successful_siblings(self):
        good_id = self.add_report("good")
        failed_id = self.add_report("failed")
        later_id = self.add_report("later")

        async def analyze(**kwargs):
            self.llm.calls.append({"complete": kwargs["title"] != "failed"})
            return complete_analysis()

        self.agent.analyze_report_directly.side_effect = analyze
        result = await batch_analysis.run_analysis(
            batch_analysis.AnalysisOptions(all=True)
        )
        self.assertEqual(result["selected"], 3)
        self.assertEqual(result["analyzed"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["failed_ids"], [failed_id])
        self.assertIsNotNone(self.analysis(good_id))
        self.assertIsNotNone(self.analysis(later_id))
        self.assertIsNone(self.analysis(failed_id))
        self.llm._client.close.assert_awaited_once()

    async def test_force_refreshes_cache_but_failed_refresh_preserves_old_analysis(
        self,
    ):
        report_id = self.add_report("cached")
        await batch_analysis.run_analysis(
            batch_analysis.AnalysisOptions(report_id=report_id)
        )
        self.agent.analyze_report_directly.reset_mock()
        initial = await batch_analysis.run_analysis(
            batch_analysis.AnalysisOptions(report_id=report_id)
        )
        self.assertEqual(initial["skipped_ids"], [report_id])
        self.assertEqual(self.agent.analyze_report_directly.await_count, 0)
        refreshed = await batch_analysis.run_analysis(
            batch_analysis.AnalysisOptions(report_id=report_id, force=True)
        )
        self.assertEqual(refreshed["analyzed_ids"], [report_id])
        saved = self.analysis(report_id)
        self.agent.analyze_report_directly.side_effect = RuntimeError(
            "model unavailable"
        )
        failed = await batch_analysis.run_analysis(
            batch_analysis.AnalysisOptions(report_id=report_id, force=True)
        )
        self.assertEqual(failed["failed_ids"], [report_id])
        self.assertEqual(self.analysis(report_id), saved)

    async def test_standard_does_not_reuse_quick_cache_and_same_depth_reuses(self):
        report_id = self.add_report("depth cache")
        await batch_analysis.run_analysis(
            batch_analysis.AnalysisOptions(report_id=report_id, depth="quick")
        )
        standard = await batch_analysis.run_analysis(
            batch_analysis.AnalysisOptions(report_id=report_id, depth="standard")
        )
        self.assertEqual(standard["analyzed_ids"], [report_id])
        repeated = await batch_analysis.run_analysis(
            batch_analysis.AnalysisOptions(report_id=report_id, depth="standard")
        )
        self.assertEqual(repeated["skipped_ids"], [report_id])

    async def test_content_change_invalidates_otherwise_valid_analysis_cache(self):
        report_id = self.add_report("changed evidence")
        await batch_analysis.run_analysis(
            batch_analysis.AnalysisOptions(report_id=report_id)
        )
        with self.factory() as session:
            session.get(
                Report, report_id
            ).abstract = "Updated study evaluates dynamic portfolios with transaction costs and a new out-of-sample comparison."
            session.commit()
        result = await batch_analysis.run_analysis(
            batch_analysis.AnalysisOptions(report_id=report_id)
        )
        self.assertEqual(result["analyzed_ids"], [report_id])

    async def test_model_change_invalidates_batch_cache(self):
        report_id = self.add_report("changed model")
        await batch_analysis.run_analysis(
            batch_analysis.AnalysisOptions(report_id=report_id)
        )
        with patch(
            "reportagent.services.batch_analysis.get_llm_model",
            return_value="new-model",
        ):
            result = await batch_analysis.run_analysis(
                batch_analysis.AnalysisOptions(report_id=report_id)
            )
        self.assertEqual(result["analyzed_ids"], [report_id])

    async def test_shared_summary_prompt_change_invalidates_batch_cache(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        report_id = self.add_report("changed prompt")
        await batch_analysis.run_analysis(
            batch_analysis.AnalysisOptions(report_id=report_id)
        )
        original = batch_analysis.skill_resource
        with TemporaryDirectory() as directory:
            changed = Path(directory) / "summary.yaml"
            changed.write_text(
                original("analyze-report", "summary.yaml").read_text()
                + "\n# revised prompt policy"
            )
            with patch.object(
                batch_analysis,
                "skill_resource",
                side_effect=lambda skill, name: changed
                if name == "summary.yaml"
                else original(skill, name),
            ):
                result = await batch_analysis.run_analysis(
                    batch_analysis.AnalysisOptions(report_id=report_id)
                )
        self.assertEqual(result["analyzed_ids"], [report_id])

    async def test_legacy_unfingerprinted_analysis_is_retryable(self):
        report_id = self.add_report(
            "legacy", cached=complete_analysis().model_dump_json()
        )
        result = await batch_analysis.run_analysis(
            batch_analysis.AnalysisOptions(report_id=report_id)
        )
        self.assertEqual(result["analyzed_ids"], [report_id])
        self.assertTrue(self.analysis(report_id)["analysis_fingerprint"])

    async def test_abstract_evidence_is_persisted_and_full_text_flag_is_respected(self):
        abstract_id = self.add_report(
            "abstract", full_text="摘要复制内容", has_full_text=False
        )
        full_id = self.add_report(
            "full",
            full_text="We evaluate actual full-text portfolio methods and report out-of-sample evidence.",
            has_full_text=True,
        )
        result = await batch_analysis.run_analysis(
            batch_analysis.AnalysisOptions(all=True)
        )
        self.assertEqual(self.analysis(abstract_id)["evidence_scope"], "abstract")
        self.assertEqual(self.analysis(full_id)["evidence_scope"], "full_text")
        self.assertEqual(result["evidence_scope_by_id"][abstract_id], "abstract")
        self.assertEqual(result["evidence_scope_by_id"][full_id], "full_text")

    async def test_empty_text_is_skipped_and_no_matches_returns_audit(self):
        report_id = self.add_report("empty", abstract="", full_text=" ")
        result = await batch_analysis.run_analysis(
            batch_analysis.AnalysisOptions(all=True)
        )
        self.assertEqual(result["skipped_ids"], [report_id])
        self.assertEqual(result["analyzed"], 0)
        empty = await batch_analysis.run_analysis(
            batch_analysis.AnalysisOptions(topic="no_such_topic")
        )
        self.assertEqual(empty["selected"], 0)
        self.assertEqual(empty["failed_ids"], [])

    async def test_recent_and_unanalyzed_keep_existing_selection_semantics(self):
        self.add_report("cached old", cached=complete_analysis().model_dump_json())
        new_id = self.add_report("new")
        latest = await batch_analysis.run_analysis(
            batch_analysis.AnalysisOptions(recent=1)
        )
        self.assertEqual(latest["report_ids"], [new_id])
        pending_id = self.add_report("pending")
        pending = await batch_analysis.run_analysis(
            batch_analysis.AnalysisOptions(unanalyzed=True)
        )
        self.assertEqual(pending["report_ids"], [pending_id])

    async def test_placeholder_abstract_is_skipped_before_any_model_call(self):
        report_id = self.add_report(
            "placeholder only", abstract="\\...", full_text=None
        )
        result = await batch_analysis.run_analysis(
            batch_analysis.AnalysisOptions(all=True)
        )
        self.assertEqual(result["analyzed"], 0)
        self.assertEqual(result["skipped_ids"], [report_id])
        self.agent.analyze_report_directly.assert_not_awaited()
        self.assertIsNone(self.analysis(report_id))


class BatchAnalysisCLITests(unittest.TestCase):
    def test_cli_topic_is_exclusive_and_forwards_force_and_depth(self):
        from reportagent.cli.analyze import main

        runner = AsyncMock(return_value={"selected": 0})
        with (
            patch.object(batch_analysis, "run_analysis", runner),
            patch(
                "sys.argv",
                ["analyze", "--topic", "factor_model", "--force", "--depth", "deep"],
            ),
        ):
            main()
        options = runner.await_args.args[0]
        self.assertEqual(options.topic, "factor_model")
        self.assertTrue(options.force)
        self.assertEqual(options.depth, "deep")
        with patch("sys.argv", ["analyze", "--topic", "factor_model", "--all"]):
            with self.assertRaises(SystemExit) as error:
                main()
        self.assertEqual(error.exception.code, 2)
