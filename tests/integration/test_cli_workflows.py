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
from tests.support.collection_scoring import RelevanceModel


@pytest.mark.parametrize(
    "name, flag",
    [
        ("collect", "--no-analysis"),
        ("analyze", "--unanalyzed"),
        ("classify", "--hybrid"),
        ("llm_filter", "--published-days"),
        ("llm_direction_filter", "--report-ids"),
        ("narrative_brief", "--force"),
        ("daily_feishu_push", "--max-reviews"),
        ("weekly_report", "--no-llm"),
        ("scheduled_collect", "--help"),
        ("reproduction", "--dry-run"),
        ("parse_pdf", "--save"),
        ("db_query", "--update-knowledge-card"),
    ],
)
def test_cli_commands_offer_documented_options(name, flag):
    result = subprocess.run(
        [sys.executable, "-m", f"reportagent.cli.{name}", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert flag in result.stdout


def test_collection_is_callable_without_processes_and_stores_real_rows():
    module = "reportagent.services.collection"
    assert importlib.util.find_spec(module), (
        "Collection must be callable outside scripts"
    )
    collection = importlib.import_module(module)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    paper = SearchResult(
        title="Portfolio optimization with transaction costs",
        abstract="A quantitative finance portfolio risk model.",
        source=SourceType.ARXIV,
    )
    source = SimpleNamespace(
        source_type=SourceType.ARXIV,
        is_available=lambda: True,
        search=AsyncMock(return_value=[paper]),
    )
    try:
        with (
            patch.object(collection, "init_db"),
            patch.object(collection, "get_session_factory", return_value=factory),
            patch.object(collection, "build_sources", return_value=[source]),
            patch.object(collection, "LLMClient", side_effect=RelevanceModel),
            patch.object(collection, "LLMClassifier", return_value=None),
        ):
            result = asyncio.run(
                collection.collect_reports(
                    collection.CollectionOptions(
                        topics="组合优化", sources="arxiv", no_analysis=True
                    )
                )
            )
        assert result["topics"] == ["组合优化"]
        assert result["sources"] == ["arxiv"]
        assert result["storage"]["added"] == 1
        assert result["results_count"] == 1
        with factory() as session:
            assert session.query(Report).one().title == paper.title
        assert json.loads(json.dumps(result))["storage"]["errors"] == []
    finally:
        engine.dispose()


def test_weekly_renders_current_shared_template_without_model_rewrite(tmp_path):
    from reportagent.skills import registry
    from reportagent.services import weekly_report

    # Copy the other skills needed for evidence and rubric fingerprints.
    import shutil

    shutil.copytree(ROOT / "skills", tmp_path / "skills")
    target = tmp_path / "skills/weekly-report/briefing.md"
    target.write_text("Weekly {date_label}: {overview}\n{other_papers}")
    papers = [{"id": 1, "title": "Fixture paper", "source": "arxiv"}]
    with patch.object(registry, "PROJECT_ROOT", tmp_path):
        before = weekly_report.generate_weekly_report(
            papers, "2026-10-05", "2026-10-11"
        )
        target.write_text("UPDATED " + target.read_text())
        after = weekly_report.generate_weekly_report(papers, "2026-10-05", "2026-10-11")
    assert "Fixture paper" in before
    assert "重点推荐 0 篇" in before
    assert after == "UPDATED " + before


def test_sync_public_url_uses_last_tunnel_address_and_preserves_other_settings(
    tmp_path,
):
    module = "reportagent.services.public_url"
    assert importlib.util.find_spec(module), (
        "URL synchronization must be callable without a shell"
    )
    service = importlib.import_module(module)
    (tmp_path / "data").mkdir()
    env = tmp_path / ".env"
    env.write_text("SOME_SETTING=keep-me\nALPHAREPORT_BASE_URL=https://old.example\n")
    (tmp_path / "data/cloudflared.log").write_text(
        "https://first.trycloudflare.com\nhttps://last.trycloudflare.com"
    )
    assert service.sync_public_url(tmp_path) is True
    assert (
        env.read_text()
        == "SOME_SETTING=keep-me\nALPHAREPORT_BASE_URL=https://last.trycloudflare.com\n"
    )
    assert env.stat().st_mode & 0o777 == 0o600
    assert service.sync_public_url(tmp_path / "missing") is False


def test_scoring_reads_shared_templates_and_keeps_failed_batches_retryable(tmp_path):
    from reportagent.services import paper_scoring as scoring
    from reportagent.skills import registry

    bundle = tmp_path / "skills/paper-selection"
    bundle.mkdir(parents=True)
    (bundle / "SKILL.md").write_text(
        "---\nname: paper-selection\ndescription: Score papers\n---\nScore."
    )
    (bundle / "scoring-system.md").write_text("Shared rubric: {directions_desc}")
    (bundle / "scoring-user.md").write_text(
        "Candidate: {papers_text}\nDirections: {direction_ids}"
    )
    prompts = []

    class OfflineModel:
        async def chat_json(self, messages, **kwargs):
            prompts.append(messages)
            if len(prompts) == 2:
                raise RuntimeError("temporary failure")
            return [{"index": 0, "score": 8.0, "reason": "Relevant"}]

    papers = [
        {
            "title": "Portfolio paper",
            "abstract": "We study portfolio allocation with risk constraints and compare forecasting models.",
        },
        {
            "title": "Retry paper",
            "abstract": "We study portfolio allocation with risk constraints and compare forecasting models.",
        },
    ]
    with patch.object(registry, "PROJECT_ROOT", tmp_path):
        results, failures = asyncio.run(
            scoring.score_papers_batch(OfflineModel(), papers, batch_size=1)
        )
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
    (bundle / "SKILL.md").write_text(
        "---\nname: paper-selection\ndescription: Match directions\n---\nScore."
    )
    (bundle / "scoring-system.md").write_text("Shared directions: {directions_desc}")
    (bundle / "scoring-user.md").write_text(
        "Papers: {papers_text}\nDirections: {direction_ids}"
    )
    with patch.object(registry, "PROJECT_ROOT", tmp_path):
        filt = DirectionFilter()
        prompt = filt.build_scoring_prompt(
            [{"id": 42, "title": "Fixture portfolio paper"}]
        )
    assert prompt.startswith("Shared directions:")
    assert "A股多因子选股" in prompt
    assert "论文 #0" in prompt
    assert "Fixture portfolio paper" in prompt
