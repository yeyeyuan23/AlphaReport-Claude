import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from reportagent.bot import feishu as bot
from reportagent.bot import claude
from reportagent.api import feishu as server, frontend
from reportagent.main import create_app


class HTTPBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        self.dist = self.root / "frontend" / "dist"
        self.dist.mkdir(parents=True)
        (self.dist / "index.html").write_text("<html>library</html>")
        (self.root / "outside.txt").write_text("private fixture")
        self.patches = [
            patch.object(server, "PROJECT_ROOT", self.root),
            patch.object(frontend, "FRONTEND_DIST", self.dist),
            patch.dict(os.environ, {
                "FEISHU_HTTP_CALLBACK_ENABLED": "true",
                "FEISHU_VERIFICATION_TOKEN": "verification-fixture",
                "FEISHU_INBOX_TOKEN": "inbox-fixture",
                "FEISHU_ALLOWED_OPEN_IDS": "ou_allowed",
            }),
        ]
        for item in self.patches:
            item.start()
        self.client = TestClient(create_app())

    def tearDown(self):
        self.client.close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def event(self, token="verification-fixture", sender="ou_allowed"):
        return {"header": {"token": token}, "event": {
            "sender": {"sender_id": {"open_id": sender}},
            "message": {"message_id": "om_fixture", "chat_id": "oc_fixture",
                        "chat_type": "p2p", "content": json.dumps({"text": "hello"})},
        }}

    def test_callback_disabled_by_default(self):
        with patch.dict(os.environ, {"FEISHU_HTTP_CALLBACK_ENABLED": "false"}):
            self.assertEqual(self.client.post("/api/v1/feishu/callback", json=self.event()).status_code, 404)

    def test_unsigned_and_disallowed_sender_cannot_enqueue(self):
        for payload in [self.event(token=""), self.event(sender="ou_other")]:
            self.assertEqual(self.client.post("/api/v1/feishu/callback", json=payload).status_code, 403)

    def test_verified_challenge_and_event_deduplication(self):
        response = self.client.post("/api/v1/feishu/callback", json={
            "token": "verification-fixture", "challenge": "challenge-fixture"})
        self.assertEqual(response.json(), {"challenge": "challenge-fixture"})
        for _ in range(2):
            self.assertEqual(self.client.post("/api/v1/feishu/callback", json=self.event()).status_code, 200)
        headers = {"Authorization": "Bearer inbox-fixture"}
        response = self.client.post("/api/v1/feishu/inbox", headers=headers)
        self.assertEqual([m["message_id"] for m in response.json()["data"]], ["om_fixture"])
        self.client.post("/api/v1/feishu/callback", json=self.event())
        self.assertEqual(self.client.post("/api/v1/feishu/inbox", headers=headers).json()["data"], [])

    def test_inbox_rejects_unauthenticated_reads_without_consuming(self):
        self.client.post("/api/v1/feishu/callback", json=self.event())
        self.assertEqual(self.client.post("/api/v1/feishu/inbox").status_code, 403)
        response = self.client.post("/api/v1/feishu/inbox", headers={"Authorization": "Bearer inbox-fixture"})
        self.assertEqual(len(response.json()["data"]), 1)

    def test_static_root_blocks_traversal_symlinks_and_unknown_api(self):
        (self.dist / "leak.txt").symlink_to(self.root / "outside.txt")
        for path in ["/..%2f..%2foutside.txt", "/leak.txt", "/api/v1/does-not-exist"]:
            self.assertEqual(self.client.get(path).status_code, 404, path)
        self.assertEqual(self.client.get("/library/12").status_code, 200)


class BotBoundaryTests(unittest.TestCase):
    def test_inbox_request_uses_configured_port_and_authentication(self):
        with patch.dict(os.environ, {"ALPHAREPORT_PORT": "8765", "FEISHU_INBOX_TOKEN": "inbox-fixture"}):
            request = bot.make_inbox_request()
        self.assertEqual(request.full_url, "http://127.0.0.1:8765/api/v1/feishu/inbox")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer inbox-fixture")

    def test_disallowed_websocket_sender_cannot_dispatch(self):
        event = SimpleNamespace(event=SimpleNamespace(
            message=SimpleNamespace(message_id="om_fixture", chat_id="oc_fixture", chat_type="p2p",
                                    content=json.dumps({"text": "/report 2005.13665"})),
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_other"))))
        with patch.dict(os.environ, {"FEISHU_ALLOWED_OPEN_IDS": "ou_allowed"}), patch.object(
            bot, "handle_cmd", side_effect=AssertionError("unauthorized dispatch")
        ), patch.object(bot, "react") as react:
            bot.on_message(event)
            react.assert_not_called()

    def test_normal_claude_call_has_permission_boundary_and_timeout(self):
        observed = {}
        def run(cmd, **kwargs):
            observed.update(cmd=cmd, **kwargs)
            return subprocess.CompletedProcess(cmd, 0, "answer", "")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fixture-key", "DEEPSEEK_BASE_URL": "https://api.deepseek.com/anthropic", "ANTHROPIC_AUTH_TOKEN": "wrong-token"}), patch.object(claude.shutil, "which", return_value="claude"), patch.object(
            claude, "get_session_snapshot", return_value=(None, "", 0)
        ), patch.object(claude, "save_session"), patch.object(claude.subprocess, "run", side_effect=run):
            self.assertEqual(claude.ask_claude("hello", "oc_fixture"), "answer")
        self.assertNotIn("--dangerously-skip-permissions", observed["cmd"])
        self.assertGreater(observed["timeout"], 0)
        self.assertEqual(observed["env"]["ANTHROPIC_API_KEY"], "fixture-key")
        self.assertEqual(observed["env"]["ANTHROPIC_AUTH_TOKEN"], "fixture-key")
        self.assertEqual(observed["env"]["ANTHROPIC_BASE_URL"], "https://api.deepseek.com/anthropic")


if __name__ == "__main__":
    unittest.main()
