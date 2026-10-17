import unittest
from unittest.mock import patch

from reportagent.bot import feishu as bot


class BotCommandTests(unittest.TestCase):
    def test_daily_is_private_and_uses_existing_job_lock(self):
        with patch.object(bot.threading, "Thread") as thread:
            self.assertIn("私聊", bot.handle_cmd("/daily", "oc_test", "group"))
            thread.assert_not_called()
            try:
                self.assertIn("开始", bot.handle_cmd("/daily", "oc_test", "p2p"))
                self.assertIn(
                    "正在", bot.handle_cmd("/report 2005.13665", "oc_test", "p2p")
                )
                self.assertEqual(thread.call_args.kwargs["args"], ("oc_test",))
            finally:
                if bot._report_lock.locked():
                    bot._report_lock.release()

    def test_report_command_rejects_invalid_source_and_group(self):
        with patch.object(bot.threading, "Thread") as thread:
            self.assertIn(
                "编号",
                bot.handle_cmd("/report https://evil.test/file", "oc_test", "p2p"),
            )
            self.assertIn(
                "私聊", bot.handle_cmd("/report 2005.13665", "oc_test", "group")
            )
            thread.assert_not_called()

    def test_report_command_starts_only_one_job(self):
        with patch.object(bot.threading, "Thread") as thread:
            try:
                self.assertIn(
                    "开始", bot.handle_cmd("/report 2005.13665", "oc_test", "p2p")
                )
                self.assertIn(
                    "正在", bot.handle_cmd("/report 2005.13665", "oc_test", "p2p")
                )
                self.assertEqual(thread.call_count, 1)
            finally:
                if bot._report_lock.locked():
                    bot._report_lock.release()


if __name__ == "__main__":
    unittest.main()
