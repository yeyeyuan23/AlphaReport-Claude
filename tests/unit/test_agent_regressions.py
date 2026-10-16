import unittest
from unittest.mock import AsyncMock, patch

from reportagent.chat import graph


class ArgumentResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_search_result_blocks_analysis_instead_of_using_report_one(self):
        step = {"step": 2, "tool": "analyze_report", "arguments": {"report_id": None}}
        tool = AsyncMock()
        with patch.dict(graph._TOOLS_BY_NAME, {"analyze_report": tool}):
            result = await graph._execute_step(step, [{"status": "done", "result": "未找到匹配的研报。"}, step])
        self.assertNotEqual(result["status"], "done")
        self.assertIn("report_id", result["result"])
        tool.ainvoke.assert_not_awaited()

    def test_unique_result_id_is_used_and_ambiguous_results_are_rejected(self):
        self.assertEqual(graph._resolve_dependent_args({"report_id": None}, [{"status": "done", "result": "[42] Paper"}], 1), {"report_id": 42})
        with self.assertRaises(ValueError):
            graph._resolve_dependent_args({"report_id": None}, [{"status": "done", "result": "[42] Paper A\n[43] Paper B"}], 1)

    def test_new_empty_search_cannot_reuse_older_search_id(self):
        with self.assertRaises(ValueError):
            graph._resolve_dependent_args({"report_id": None}, [
                {"status": "done", "result": "[42] Old paper"},
                {"status": "done", "result": "未找到匹配的研报。"},
            ], 2)

    def test_failed_or_skipped_prerequisite_cannot_reuse_older_report_id(self):
        for status in ["error", "skipped", "pending"]:
            with self.subTest(status=status), self.assertRaises(ValueError):
                graph._resolve_dependent_args({"report_id": None}, [
                    {"status": "done", "result": "[42] Old paper"},
                    {"status": status, "result": "执行失败或跳过"},
                ], 2)


if __name__ == "__main__":
    unittest.main()
