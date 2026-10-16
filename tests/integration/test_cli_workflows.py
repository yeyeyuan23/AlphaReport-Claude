"""Executable boundaries: canonical commands, callable workflows and shared resources."""
import asyncio
import importlib
import importlib.util
import json
import subprocess
import sys
from tests.support.paths import PROJECT_ROOT as ROOT
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.models.database import Base, Report
from reportagent.models.schemas import SearchResult, SourceType



@pytest.mark.parametrize("name, flag", [
    ("collect", "--no-analysis"), ("analyze", "--unanalyzed"),
    ("classify", "--hybrid"), ("llm_filter", "--published-days"),
    ("llm_direction_filter", "--report-ids"), ("narrative_brief", "--force"),
    ("daily_feishu_push", "--no-collect"), ("weekly_report", "--no-llm"),
    ("parse_pdf", "--save"), ("db_query", "--update-knowledge-card"),
])
def test_cli_commands_offer_documented_options(name, flag):
    result = subprocess.run([sys.executable, "-m", f"reportagent.cli.{name}", "--help"],
                            cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert flag in result.stdout


def test_collection_is_callable_without_processes_and_stores_real_rows():
    module = "reportagent.services.collection"
    assert importlib.util.find_spec(module), "Collection must be callable outside scripts"
    collection = importlib.import_module(module)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    paper = SearchResult(title="Portfolio optimization with transaction costs",
                         abstract="A quantitative finance portfolio risk model.",
                         source=SourceType.ARXIV)
    source = SimpleNamespace(source_type=SourceType.ARXIV,
                             is_available=lambda: True, search=AsyncMock(return_value=[paper]))
    try:
        with patch.object(collection, "init_db"), patch.object(
            collection, "get_session_factory", return_value=factory
        ), patch.object(collection, "build_sources", return_value=[source]), patch.object(
            collection, "LLMClient", side_effect=RuntimeError("No model in offline test")
        ):
            result = asyncio.run(collection.collect_reports(collection.CollectionOptions(
                topics="组合优化", sources="arxiv", no_analysis=True)))
        assert result["topics"] == ["组合优化"]
        assert result["sources"] == ["arxiv"]
        assert result["storage"]["added"] == 1
        assert result["results_count"] == 1
        with factory() as session:
            assert session.query(Report).one().title == paper.title
        assert json.loads(json.dumps(result))["storage"]["errors"] == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("module, skill, filename, template, invoke", [
    ("weekly_report", "weekly-report", "briefing.md",
     "Weekly {week_start}/{week_end}, {total_count}: {papers_json}",
     lambda service: service.generate_weekly_report([{"title": "Fixture paper"}], "2026-10-05", "2026-10-11")),
])
def test_delivery_generation_renders_the_current_shared_skill_resource(
    tmp_path, module, skill, filename, template, invoke
):
    from reportagent.skills import registry
    service = importlib.import_module(f"reportagent.services.{module}")
    bundle = tmp_path / "skills" / skill
    bundle.mkdir(parents=True)
    (bundle / "SKILL.md").write_text(f"---\nname: {skill}\ndescription: Test task\n---\nUse the shared resource.")
    (bundle / filename).write_text(template)
    prompts = []

    def generate(command, **kwargs):
        prompts.append(command[command.index("-p") + 1])
        return subprocess.CompletedProcess(command, 0, "Generated report", "")

    with patch.object(registry, "PROJECT_ROOT", tmp_path), patch(
        "shutil.which", return_value="/fixture/claude"
    ), patch.object(service.subprocess, "run", side_effect=generate):
        assert invoke(service) == "Generated report"
        (bundle / filename).write_text("UPDATED " + template)
        assert invoke(service) == "Generated report"
    assert "Fixture paper" in prompts[0]
    assert "{papers_json}" not in prompts[0]
    assert prompts[1].startswith("UPDATED ")


def test_sync_public_url_uses_last_tunnel_address_and_preserves_other_settings(tmp_path):
    module = "reportagent.services.public_url"
    assert importlib.util.find_spec(module), "URL synchronization must be callable without a shell"
    service = importlib.import_module(module)
    (tmp_path / "data").mkdir()
    env = tmp_path / ".env"
    env.write_text("SOME_SETTING=keep-me\nALPHAREPORT_BASE_URL=https://old.example\n")
    (tmp_path / "data/cloudflared.log").write_text(
        "https://first.trycloudflare.com\nhttps://last.trycloudflare.com")
    assert service.sync_public_url(tmp_path) is True
    assert env.read_text() == "SOME_SETTING=keep-me\nALPHAREPORT_BASE_URL=https://last.trycloudflare.com\n"
    assert env.stat().st_mode & 0o777 == 0o600
    assert service.sync_public_url(tmp_path / "missing") is False


def test_scoring_reads_shared_templates_and_keeps_failed_batches_retryable(tmp_path):
    from reportagent.services import paper_scoring as scoring
    from reportagent.skills import registry

    bundle = tmp_path / "skills/paper-selection"
    bundle.mkdir(parents=True)
    (bundle / "SKILL.md").write_text("---\nname: paper-selection\ndescription: Score papers\n---\nScore.")
    (bundle / "scoring-system.md").write_text("Shared rubric: {directions_desc}")
    (bundle / "scoring-user.md").write_text("Candidate: {papers_text}\nDirections: {direction_ids}")
    prompts = []

    class OfflineModel:
        async def chat_json(self, messages, **kwargs):
            prompts.append(messages)
            if len(prompts) == 2:
                raise RuntimeError("temporary failure")
            return [{"index": 0, "score": 8.0, "reason": "Relevant"}]

    papers = [{"title": "Portfolio paper", "abstract": "Test"},
              {"title": "Retry paper", "abstract": "Test"}]
    with patch.object(registry, "PROJECT_ROOT", tmp_path):
        results, failures = asyncio.run(scoring.score_papers_batch(OfflineModel(), papers, batch_size=1))
    assert len(results) == 1
    assert results[0]["_paper"]["title"] == "Portfolio paper"
    assert results[0]["score"] == 8.0
    assert failures == [(1, 1, "temporary failure")]
    assert prompts[0][0]["content"].startswith("Shared rubric:")
    assert "Candidate: ### 论文 #0" in prompts[0][1]["content"]


def test_direction_filter_uses_shared_template_with_configured_directions(tmp_path):
    from reportagent.filters.direction_filter import DirectionFilter
    from reportagent.skills import registry

    bundle = tmp_path / "skills/paper-selection"
    bundle.mkdir(parents=True)
    (bundle / "SKILL.md").write_text("---\nname: paper-selection\ndescription: Match directions\n---\nScore.")
    (bundle / "direction-scoring.md").write_text("Shared directions: {directions_desc}\nPapers: {papers_desc}")
    with patch.object(registry, "PROJECT_ROOT", tmp_path):
        filt = DirectionFilter()
        prompt = filt.build_scoring_prompt([{"id": 42, "title": "Fixture portfolio paper"}])
    assert prompt.startswith("Shared directions:")
    assert "A股多因子选股" in prompt
    assert "ID: 42" in prompt
    assert "Fixture portfolio paper" in prompt
