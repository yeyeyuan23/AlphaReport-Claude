import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from reportagent.api import frontend
from reportagent.main import create_app
from reportagent.skills import registry


class WebEntrypointTests(unittest.TestCase):
    def test_clean_checkout_can_serve_api_without_frontend_build(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(frontend, "FRONTEND_DIST", Path(directory) / "missing"),
        ):
            client = TestClient(create_app())
            self.assertEqual(
                client.get("/api/v1/health").json()["data"]["server"], "ok"
            )
            self.assertEqual(client.get("/api/v1/unknown").status_code, 404)
            self.assertIn("not built", client.get("/").json()["message"])
            client.close()

    def test_web_skill_catalog_reads_the_same_library(self):
        app = create_app()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(registry, "PROJECT_ROOT", Path(directory)),
        ):
            skill = Path(directory) / "skills" / "fixture"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: fixture\ndescription: shared-web-description\n---\nInstructions"
            )
            client = TestClient(app)
            response = client.get("/api/v1/skills")
            self.assertEqual(
                response.json()["data"],
                [{"name": "fixture", "description": "shared-web-description"}],
            )
            client.close()

    def test_reader_api_surface_excludes_chat_collection_tasks_and_mail(self):
        app = create_app()
        paths = set(app.openapi()["paths"])
        self.assertIn("/api/v1/reports", paths)
        self.assertIn("/api/v1/reports/{report_id}/pdf", paths)
        self.assertFalse(
            any(
                retired in path
                for path in paths
                for retired in (
                    "/chat",
                    "/collection/tasks",
                    "/subscriptions",
                    "/email",
                )
            )
        )
        client = TestClient(app)
        self.addCleanup(client.close)
        self.assertEqual(client.get("/api/v1/collection/tasks/list").status_code, 404)
        self.assertIn(client.post("/api/v1/chat", json={}).status_code, {404, 405})
