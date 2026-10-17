"""Daily-run boundaries using only temporary files, SQLite and offline adapters."""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.models.database import Base, Report, ReportRun
from reportagent.services import daily_push as daily
from reportagent.services.report_quality import review_fingerprint
from reportagent.services.paper_scoring import evidence_fingerprint


def save_offline_assessment(session, row, assessment):
    assessment.update(model="fixture-model", fingerprint=review_fingerprint(vars(row), "fixture-model"))
    row.evaluation_json = json.dumps(assessment)
    session.commit()


@pytest.fixture
def offline_daily(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/push_directions.yaml").write_text("push_settings: {}")
    monkeypatch.setattr(daily, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(daily, "init_db", lambda: None)
    monkeypatch.setattr(daily, "get_session_factory", lambda: factory)
    monkeypatch.setattr(daily, "sync_public_url", lambda: None)
    monkeypatch.setattr(daily, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setattr(daily, "load_enabled_directions", lambda: {"portfolio": {}})
    monkeypatch.setattr(daily.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(daily, "send_feishu_card", lambda *args, **kwargs: True)
    monkeypatch.delenv("FEISHU_DAILY_PUSH_IDS", raising=False)

    async def review(ids, **kwargs):
        assessments = {}
        with factory() as session:
            for rid in ids[:kwargs.get("max_reviews", 12)]:
                assessment = {"tier": "browse", "label": "值得浏览", "status": "reviewed",
                              "findings": [], "limitations": ["Offline evidence"],
                              "source_category": "academic"}
                save_offline_assessment(session, session.get(Report, rid), assessment)
                assessments[rid] = assessment
        return assessments
    monkeypatch.setattr(daily, "review_report_ids", review)
    yield factory, tmp_path
    engine.dispose()


def insert_paper(factory):
    with factory() as session:
        row = Report(id=1, title="Old title", source="arxiv", content_hash="one",
                     abstract="We study portfolio allocation and compare rolling trading policies with transaction costs.",
                     source_url="https://arxiv.org/abs/2610.00001v1", arxiv_id="2610.00001v1",
                     published_date=datetime.now(timezone.utc), selection_score=8,
                     selection_relevance=8, selection_direction="portfolio")
        row.selection_provenance_json = json.dumps({"fingerprint": daily.scoring_fingerprint(),
            "status": "scored", "evidence_fingerprint": evidence_fingerprint(vars(row))})
        session.add(row)
        session.commit()


def read_saved_run(factory):
    with factory() as session:
        row = session.query(ReportRun).one()
        return json.loads(row.result_json)


def assert_final_artifact(result):
    assert result.get("run_artifact_path"), "Each run must expose its final JSON artifact"
    saved = json.loads(Path(result["run_artifact_path"]).read_text())
    assert saved["status"] == result["status"]
    assert saved["completed_at"] == result["completed_at"]
    assert saved["deliveries"] == result["deliveries"]
    assert saved["status"] != "running"
    return saved


def test_failed_relevance_without_candidates_cannot_be_successful_empty(offline_daily, monkeypatch):
    factory, _ = offline_daily
    monkeypatch.setattr(daily.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess([], 1, "", "fixture failure"))
    with pytest.raises(RuntimeError, match="相关性"):
        daily.run_daily_push(daily.DailyPushOptions(no_collect=True, dry_run=True))
    result = read_saved_run(factory)
    assert result["status"] == "failed"
    assert result["stages"]["relevance"]["status"] == "failed"
    assert_final_artifact(result)


@pytest.mark.parametrize("timeout", [False, True])
def test_failed_relevance_with_valid_candidates_is_explicit_partial(offline_daily, monkeypatch, timeout):
    factory, _ = offline_daily
    insert_paper(factory)
    def scoring(*args, **kwargs):
        if timeout:
            raise subprocess.TimeoutExpired("offline-scoring", 300)
        return subprocess.CompletedProcess([], 2, "", "fixture failure")
    monkeypatch.setattr(daily.subprocess, "run", scoring)
    result = daily.run_daily_push(daily.DailyPushOptions(no_collect=True, dry_run=True))
    assert result["status"] == "partial"
    assert result["outcome"] == "preview"
    assert result["stages"]["relevance"]["status"] == "failed"
    assert result["report_ids"] == [1]
    assert result["warnings"] and "相关性" in result["warnings"][0]
    assert "相关性" in Path(result["artifact_path"]).read_text()
    assert_final_artifact(result)


@pytest.mark.parametrize("dry_run", [False, True])
def test_completed_run_json_contains_final_status_and_send_results(offline_daily, dry_run):
    factory, _ = offline_daily
    insert_paper(factory)
    result = daily.run_daily_push(daily.DailyPushOptions(
        no_collect=True, report_ids="1", dry_run=dry_run, receive_ids="A"))
    assert result["status"] == ("preview" if dry_run else "delivered")
    saved = assert_final_artifact(result)
    assert saved["deliveries"] == ({} if dry_run else {"A": True})
    assert saved["papers"][0]["id"] == 1


def test_empty_daily_sends_a_completion_card_to_each_recipient(offline_daily, monkeypatch):
    factory, _ = offline_daily
    sent = []
    def send(recipient, briefing, papers, *args):
        sent.append((recipient, briefing, papers))
        return True
    monkeypatch.setattr(daily, 'send_feishu_card', send)
    result = daily.run_daily_push(daily.DailyPushOptions(no_collect=True, receive_ids='A,B,A'))
    assert [item[0] for item in sent] == ['A', 'B']
    assert all(not item[2] and '暂无符合日期范围与筛选条件' in item[1] for item in sent)
    assert result['status'] == 'empty'
    assert result['deliveries'] == {'A': True, 'B': True}
    assert_final_artifact(result)


def test_empty_daily_delivery_failure_is_not_silently_successful(offline_daily, monkeypatch):
    factory, _ = offline_daily
    monkeypatch.setattr(daily, 'send_feishu_card', lambda recipient, *args: recipient == 'A')
    with pytest.raises(RuntimeError, match='投递'):
        daily.run_daily_push(daily.DailyPushOptions(no_collect=True, receive_ids='A,B'))
    saved = read_saved_run(factory)
    assert saved['status'] == 'failed'
    assert saved['deliveries'] == {'A': True, 'B': False}
    assert_final_artifact(saved)


def test_empty_preview_has_an_explicit_result_without_sending(offline_daily, monkeypatch):
    sent = []
    monkeypatch.setattr(daily, 'send_feishu_card', lambda *args: sent.append(args))
    result = daily.run_daily_push(daily.DailyPushOptions(no_collect=True, dry_run=True))
    assert result['status'] == 'preview'
    assert '暂无符合日期范围与筛选条件' in result['briefing']
    assert sent == [] and result['deliveries'] == {}


def test_failed_delivery_json_preserves_each_recipient_send_result(offline_daily, monkeypatch):
    factory, _ = offline_daily
    insert_paper(factory)
    monkeypatch.setattr(daily, "send_feishu_card", lambda recipient, *args: recipient == "A")
    with pytest.raises(RuntimeError, match="投递"):
        daily.run_daily_push(daily.DailyPushOptions(no_collect=True, report_ids="1", receive_ids="A,B"))
    result = read_saved_run(factory)
    saved = assert_final_artifact(result)
    assert saved["status"] == "failed"
    assert saved["deliveries"] == {"A": True, "B": False}


@pytest.mark.parametrize("explicit_ids", [False, True])
def test_repeated_daily_runs_send_same_period_without_writing_legacy_markers(offline_daily, monkeypatch, explicit_ids):
    factory, _ = offline_daily
    insert_paper(factory)
    with factory().get_bind().begin() as conn:
        conn.exec_driver_sql("ALTER TABLE reports ADD COLUMN pushed_at TEXT")
        conn.exec_driver_sql("CREATE TABLE report_deliveries (report_id INTEGER, receive_type TEXT, receive_id TEXT, delivered_at TEXT)")
    sent = []
    monkeypatch.setattr(daily, "send_feishu_card", lambda recipient, brief, papers, *args:
                        sent.append((recipient, [p["id"] for p in papers])) or True)
    options = daily.DailyPushOptions(no_collect=True, receive_ids="A,B,A",
                                     report_ids="1" if explicit_ids else "")
    for _ in range(2):
        result = daily.run_daily_push(options)
        assert result["report_ids"] == [1] and result["status"] == "delivered"
    assert sent == [("A", [1]), ("B", [1]), ("A", [1]), ("B", [1])]
    with factory().get_bind().connect() as conn:
        assert conn.exec_driver_sql("SELECT pushed_at FROM reports WHERE id=1").scalar() is None
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM report_deliveries").scalar() == 0


def test_default_daily_generates_from_library_without_triggering_collection(offline_daily, monkeypatch):
    from reportagent.services import collection
    factory, _ = offline_daily
    insert_paper(factory)
    def collection_forbidden(*args, **kwargs):
        pytest.fail("Generating a daily report must never fetch source data")
    monkeypatch.setattr(collection, "collect_reports", collection_forbidden)
    def run(command, **kwargs):
        assert "reportagent.cli.collect" not in command
        return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(daily.subprocess, "run", run)
    result = daily.run_daily_push(daily.DailyPushOptions(dry_run=True))
    assert result["report_ids"] == [1] and result["status"] == "preview"
    assert result["stages"]["collection"]["status"] == "not_run"


def test_failed_scheduled_collection_is_disclosed_without_blocking_library_report(offline_daily):
    factory, root = offline_daily
    insert_paper(factory)
    directory = root / "data/collections"
    directory.mkdir(parents=True)
    (directory / "latest.json").write_text(json.dumps({"run_id": "previous", "status": "failed",
        "completed_at": "2026-10-17T00:15:00+00:00", "source_statuses": [
            {"source": "arxiv", "status": "error", "valid": 0, "errors": ["ReadTimeout"]}]}))
    result = daily.run_daily_push(daily.DailyPushOptions(dry_run=True))
    assert result["status"] == "partial" and result["report_ids"] == [1]
    assert result["stages"]["collection"]["status"] == "not_run"
    assert result["collection_run"]["run_id"] == "previous"
    assert "最近定时采集" in result["briefing"] and "ReadTimeout" in result["briefing"]
    assert "本次在线采集" not in result["briefing"]
    assert_final_artifact(result)


def test_render_refreshes_candidate_metadata_without_refiltering_explicit_ids(offline_daily, monkeypatch):
    factory, _ = offline_daily
    insert_paper(factory)
    marker = {"tier": "browse", "label": "值得浏览", "status": "reviewed",
              "findings": [], "limitations": ["New revision assessed"]}
    async def review(ids, **kwargs):
        with factory() as session:
            row = session.get(Report, 1)
            row.title = "New revision title"
            row.source_url = "https://arxiv.org/abs/2610.00001v2"
            row.arxiv_id = "2610.00001v2"
            row.abstract = "Updated abstract"
            row.has_full_text = True
            # Revision invalidation must not silently remove the explicitly chosen paper.
            row.selection_score = 0
            row.selection_relevance = None
            row.selection_direction = None
            row.selection_reason = None
            row.selection_provenance_json = None
            save_offline_assessment(session, row, marker)
        return {1: marker}
    monkeypatch.setattr(daily, "review_report_ids", review)
    result = daily.run_daily_push(daily.DailyPushOptions(no_collect=True, report_ids="1", dry_run=True))
    saved = json.loads(Path(result.get("run_artifact_path", Path(result["artifact_path"]).with_suffix(".json"))).read_text())
    candidate = saved["papers"][0]
    assert candidate["title"] == "New revision title"
    assert candidate["source_url"].endswith("v2") and candidate["arxiv_id"].endswith("v2")
    assert candidate["abstract"] == "Updated abstract" and candidate["has_full_text"] is True
    assert candidate["selection_relevance"] is None and candidate["selection_direction"] is None
    assert candidate["selection_score"] == 0 and candidate["selection_reason"] == ""
    assert candidate["evaluation"] == marker
    assert candidate["ui_url"].endswith("/library/1")
    assert "New revision title" in saved["briefing"] and "Old title" not in saved["briefing"]


def test_card_footer_describes_rendered_saved_evaluations(monkeypatch):
    import lark_oapi
    client = MagicMock()
    client.im.v1.message.create.return_value.success.return_value = True
    requests = []
    def send(request):
        requests.append(json.loads(request.request_body.content))
        return client.im.v1.message.create.return_value
    client.im.v1.message.create.side_effect = send
    builder = MagicMock()
    builder.app_id.return_value.app_secret.return_value.build.return_value = client
    monkeypatch.setattr(lark_oapi.Client, "builder", lambda: builder)
    assert daily.send_feishu_card("A", "Saved evaluation", []) is True
    footer = requests[0]["elements"][-1]["elements"][0]["content"]
    assert "Claude Code" not in footer
    assert "已保存评估" in footer


def test_configured_review_budget_limits_candidates_and_explicit_option_overrides(offline_daily):
    factory, root = offline_daily
    insert_paper(factory)
    with factory() as session:
        session.add(Report(id=2, title="Second", source="arxiv", content_hash="two"))
        session.commit()
    (root / "configs/push_directions.yaml").write_text("push_settings:\n  max_reviews: 1\n")
    result = daily.run_daily_push(daily.DailyPushOptions(no_collect=True, report_ids="1,2", dry_run=True))
    assert result["paper_count"] == 1
    override = daily.run_daily_push(daily.DailyPushOptions(
        no_collect=True, report_ids="1,2", max_reviews=2, dry_run=True))
    assert override["paper_count"] == 2


def test_recipient_brief_uses_same_configured_featured_limit(offline_daily, monkeypatch):
    factory, root = offline_daily
    insert_paper(factory)
    (root / "configs/push_directions.yaml").write_text("push_settings:\n  max_featured: 2\n")
    rendered_limits = []
    original_render = daily.render_briefing
    def render(papers, date_label, **kwargs):
        rendered_limits.append(kwargs.get("max_featured"))
        return original_render(papers, date_label, **kwargs)
    monkeypatch.setattr(daily, "render_briefing", render)
    daily.run_daily_push(daily.DailyPushOptions(no_collect=True, report_ids="1", receive_ids="A,B"))
    assert rendered_limits == [2]  # One complete brief is shared by all recipients.


@pytest.mark.parametrize("change", ["cleared", "changed", "invalid_json", "revision_changed"])
def test_review_of_later_paper_cannot_mix_new_metadata_with_earlier_stale_conclusions(
    offline_daily, monkeypatch, change,
):
    factory, _ = offline_daily
    insert_paper(factory)
    with factory() as session:
        session.add(Report(id=2, title="Later paper", source="arxiv", content_hash="two"))
        session.commit()
    async def review(ids, **kwargs):
        stale = {"tier": "focus", "label": "重点阅读", "status": "reviewed",
                 "research_question": "Old revision question",
                 "method": {"claim": "Old method"},
                 "findings": [{"claim": "STALE CONCLUSION"}], "limitations": ["Old limitation"]}
        with factory() as session:
            row = session.get(Report, 1)
            save_offline_assessment(session, row, stale)
        # Report 2's review is still running when a collector updates report 1.
        with factory() as session:
            row = session.get(Report, 1)
            if change == "cleared":
                row.title, row.arxiv_id = "Newest revision", "2610.00001v3"
                row.evaluation_json = None
            elif change == "changed":
                updated = dict(stale, findings=[{"claim": "Different saved conclusion"}])
                row.evaluation_json = json.dumps(updated)
            elif change == "invalid_json":
                row.evaluation_json = "{broken"
            else:
                # A source update must be detected even if invalidation did not clear the old JSON.
                row.title, row.arxiv_id = "Newest revision", "2610.00001v3"
            session.commit()
        return {1: stale}
    monkeypatch.setattr(daily, "review_report_ids", review)
    result = daily.run_daily_push(daily.DailyPushOptions(no_collect=True, report_ids="1,2", dry_run=True))
    saved = assert_final_artifact(result)
    candidate = next(p for p in saved["papers"] if p["id"] == 1)
    assert candidate["evaluation"]["tier"] == "pending"
    assert candidate["evaluation"]["findings"] == []
    assert "STALE CONCLUSION" not in saved["briefing"]
    if change in {"cleared", "revision_changed"}:
        assert candidate["title"] == "Newest revision" and candidate["arxiv_id"].endswith("v3")
