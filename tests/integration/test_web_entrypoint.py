import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from reportagent.api import frontend
from reportagent.db.engine import get_db
from reportagent.main import create_app
from reportagent.models.database import Base, Report
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

    def test_library_uses_shared_knowledge_card_conversion(self):
        import json

        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine)
        analysis = {
            "core_contribution": "A supported contribution",
            "summary": "Measured result",
            "assessment": {"strengths": ["Evidence"]},
        }
        with factory() as session:
            report = Report(
                title="Fixture",
                source="arxiv",
                content_hash="fixture",
                analysis_json=json.dumps(analysis),
            )
            session.add(report)
            session.commit()
            report_id = report.id

        def database():
            with factory() as session:
                yield session

        app = create_app()
        app.dependency_overrides[get_db] = database
        client = TestClient(app)
        response = client.post(f"/api/v1/reports/{report_id}/knowledge-card")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["status"], "pending")
        self.assertEqual(response.json()["data"]["highlights"], [])
        self.assertIsNone(response.json()["data"]["quality_score"])
        client.close()
        engine.dispose()
