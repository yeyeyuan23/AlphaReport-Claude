"""Weekly synthesis uses every current assessment and keeps citations accountable."""

import json

import pytest

from reportagent.llm.client import LLMClient
from reportagent.services import weekly_report as weekly
from tests.integration.test_weekly_evidence import assessed, paper


def sample_papers():
    return [
        assessed(
            paper(rid=i, title=f"论文{i}"),
            findings=[{"claim": f"论文{i}的完整发现。", "quote": "source"}],
            limitations=[f"论文{i}只适用于连续价格路径。"],
        )
        for i in (1, 2, 3)
    ]


def response():
    return {
        "directions": [
            {
                "direction": "portfolio_optimization",
                "updates": [
                    {
                        "text": "三篇研究分别讨论约束与估计，结论均有适用条件。",
                        "paper_ids": [1, 2, 3],
                    }
                ],
            }
        ]
    }


def inject_model(monkeypatch, value):
    requests = []
    monkeypatch.setattr(LLMClient, "_build_anthropic_client", lambda self: None)
    monkeypatch.setattr(LLMClient, "_build_openai_client", lambda self: None)

    async def chat(self, messages, **kwargs):
        requests.append(json.loads(messages[-1]["content"]))
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(LLMClient, "chat_json", chat)
    return requests


def test_synthesis_uses_all_three_assessments_and_links_each_paper(monkeypatch):
    requests = inject_model(monkeypatch, response())
    papers = sample_papers()
    papers[2]["evaluation"]["findings"][0]["claim"] = "完整结论" * 100
    papers[2]["evaluation_json"] = json.dumps(papers[2]["evaluation"])
    text = weekly.generate_weekly_report(
        papers, "2026-10-10", "2026-10-17", summarize=True
    )
    updates = text.split("## 本周研究更新", 1)[1].split("## 收录与评估说明", 1)[0]
    assert "三篇研究分别讨论约束与估计" in updates
    assert "3 篇" in updates
    for rid in (1, 2, 3):
        assert f"http://localhost/library/{rid}" in updates
    sent = requests[0]["directions"][0]["papers"]
    assert [p["id"] for p in sent] == [1, 2, 3]
    assert sent[2]["findings"][0]["claim"] == "完整结论" * 100
    assert sent[2]["limitations"] == ["论文3只适用于连续价格路径。"]
    assert "full_text" not in sent[0]


@pytest.mark.parametrize("failure", ["omitted", "invented", "empty", "timeout"])
def test_failed_or_uncited_synthesis_falls_back_without_hiding_papers(
    monkeypatch, failure
):
    value = response()
    update = value["directions"][0]["updates"][0]
    if failure == "omitted":
        update["paper_ids"] = [1, 2]
    elif failure == "invented":
        update["paper_ids"] = [1, 2, 3, 99]
    elif failure == "empty":
        update["text"] = ""
    else:
        value = TimeoutError("model timeout")
    inject_model(monkeypatch, value)
    text = weekly.generate_weekly_report(
        sample_papers(), "2026-10-10", "2026-10-17", summarize=True
    )
    updates = text.split("## 本周研究更新", 1)[1].split("## 收录与评估说明", 1)[0]
    assert "总结未生成" in updates
    assert "三篇研究分别" not in updates
    for rid in (1, 2, 3):
        assert f"论文{rid}的完整发现" in updates
        assert f"http://localhost/library/{rid}" in updates
    assert "重点推荐 3 篇" in text


def test_stale_and_pending_assessments_are_not_sent_as_research_findings(monkeypatch):
    value = response()
    value["directions"][0]["updates"][0]["paper_ids"] = [1]
    requests = inject_model(monkeypatch, value)
    papers = sample_papers()
    papers[1]["full_text"] += " changed source"
    papers[2]["evaluation"].update(tier="pending", status="needs_verification")
    weekly.generate_weekly_report(papers, "2026-10-10", "2026-10-17", summarize=True)
    assert [p["id"] for p in requests[0]["directions"][0]["papers"]] == [1]


def test_offline_preview_lists_each_paper_even_when_findings_repeat(monkeypatch):
    requests = inject_model(monkeypatch, AssertionError("No model in offline preview"))
    papers = sample_papers()
    for p in papers:
        p["evaluation"]["findings"] = [{"claim": "同一发现"}]
    text = weekly.generate_weekly_report(papers, "2026-10-10", "2026-10-17")
    updates = text.split("## 本周研究更新", 1)[1].split("## 收录与评估说明", 1)[0]
    assert not requests
    for rid in (1, 2, 3):
        assert f"http://localhost/library/{rid}" in updates


def test_normal_delivery_requests_model_synthesis(monkeypatch):
    requests = inject_model(monkeypatch, response())
    monkeypatch.setattr(weekly, "get_weekly_papers", lambda *a: sample_papers())
    monkeypatch.setattr(weekly, "send_feishu_card", lambda *a: True)
    result = weekly.run_weekly_report(
        weekly.WeeklyReportOptions(receive_ids="authorized-test-recipient")
    )
    assert result["sent"] == 1
    assert len(requests) == 1


def test_direction_summaries_cannot_borrow_another_directions_citations(monkeypatch):
    papers = sample_papers()
    papers[2] = assessed(paper(rid=3, selection_direction="ml_quant"))
    value = response()
    value["directions"].append(
        {
            "direction": "ml_quant",
            "updates": [{"text": "另一方向总结", "paper_ids": [3]}],
        }
    )
    inject_model(monkeypatch, value)
    text = weekly.generate_weekly_report(
        papers, "2026-10-10", "2026-10-17", summarize=True
    )
    assert "总结未生成" in text
    assert "另一方向总结" not in text


def test_no_current_assessments_skips_paid_summary_call(monkeypatch):
    requests = inject_model(monkeypatch, AssertionError("Nothing to summarize"))
    text = weekly.generate_weekly_report(
        [paper()], "2026-10-10", "2026-10-17", summarize=True
    )
    assert not requests
    assert "待核验" in text
    assert "总结未生成" not in text


def test_dry_run_with_summary_generates_preview_without_delivery(monkeypatch, tmp_path):
    inject_model(monkeypatch, response())
    monkeypatch.setattr(weekly, "get_weekly_papers", lambda *a: sample_papers())

    def forbidden(*args):
        pytest.fail("Summary preview must not deliver a card")

    monkeypatch.setattr(weekly, "send_feishu_card", forbidden)
    output = tmp_path / "preview.md"
    result = weekly.run_weekly_report(
        weekly.WeeklyReportOptions(dry_run=True, with_summary=True, output=str(output))
    )
    assert result["status"] == "preview"
    assert result["sent"] == 0
    assert "三篇研究分别讨论约束与估计" in output.read_text()


def test_weekly_only_shows_direction_updates_without_duplicate_paper_lists():
    papers = sample_papers()
    papers[1]["evaluation"]["tier"] = "browse"
    text = weekly.generate_weekly_report(papers, "2026-10-10", "2026-10-17")
    assert "## 本周重点推荐" not in text
    assert "## 其他可浏览论文" not in text
    assert "选读理由摘录" not in text
    assert "## 本周研究更新" in text
    assert "## 收录与评估说明" in text
    for rid in (1, 2, 3):
        assert text.count(f"http://localhost/library/{rid}") == 1
        assert f"论文{rid}的完整发现" in text


def test_multiple_direction_updates_fall_back_without_losing_papers(monkeypatch):
    value = response()
    value["directions"][0]["updates"] = [
        {"text": "第一篇讨论约束。\n\n结论有条件。", "paper_ids": [1]},
        {"text": "另外两篇讨论估计。", "paper_ids": [2, 3]},
    ]
    inject_model(monkeypatch, value)
    text = weekly.generate_weekly_report(
        sample_papers(), "2026-10-10", "2026-10-17", summarize=True
    )
    updates = text.split("## 本周研究更新", 1)[1].split("## 收录与评估说明", 1)[0]
    assert "第一篇讨论约束" not in updates
    assert "总结未生成" in updates
    for rid in (1, 2, 3):
        assert updates.count(f"http://localhost/library/{rid}") == 1


def test_offline_direction_fallback_also_uses_one_paragraph():
    text = weekly.generate_weekly_report(sample_papers(), "2026-10-10", "2026-10-17")
    updates = text.split("**投资组合优化 · 3 篇**", 1)[1].split("## 收录与评估说明", 1)[
        0
    ]
    assert "论文1的完整发现。 论文2的完整发现。 论文3的完整发现。" in updates
