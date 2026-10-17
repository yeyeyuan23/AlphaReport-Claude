import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.support.report_fixtures import reviewed_paper, reproduction_output as output


def test_preparation_preserves_evidence_and_separates_proposal():
    from reportagent.services.reproduction import prepare_reproduction

    model = SimpleNamespace(chat_json=AsyncMock(return_value=output()))
    card = asyncio.run(prepare_reproduction(reviewed_paper(), llm=model))
    assert card["status"] == "ready"
    assert card["fields"]["target"]["evidence"][0]["raw_quote"]
    assert card["proposal_label"] == "建议验证方案，非作者实验或已完成复现"
    assert "尚未复现" in card["notice"]


def test_invalid_quote_gets_one_correction_before_persistable_success():
    from reportagent.services.reproduction import prepare_reproduction

    invalid = output()
    invalid["fields"]["target"]["evidence"][0]["quote"] = (
        "Invented text not found anywhere in the original paper."
    )
    model = SimpleNamespace(chat_json=AsyncMock(side_effect=[invalid, output()]))
    card = asyncio.run(prepare_reproduction(reviewed_paper(), llm=model))
    assert card["status"] == "ready"
    assert card["fields"]["target"]["evidence"][0]["verified"] is True
    assert "target" in model.chat_json.call_args.args[0][-1]["content"]


def test_failed_quote_correction_stops_after_one_retry():
    from reportagent.services.reproduction import prepare_reproduction

    invalid = output()
    invalid["fields"]["target"]["evidence"][0]["quote"] = (
        "Invented text not found anywhere in the original paper."
    )
    model = SimpleNamespace(chat_json=AsyncMock(return_value=invalid))
    card = asyncio.run(prepare_reproduction(reviewed_paper(), llm=model))
    assert card["status"] == "retryable_error"
    assert not card.get("fields")
    assert model.chat_json.await_count == 2


def test_preparation_needs_current_full_text_evidence():
    from reportagent.services.reproduction import prepare_reproduction

    model = SimpleNamespace(chat_json=AsyncMock())
    card = asyncio.run(prepare_reproduction({"title": "Only title"}, llm=model))
    assert card["status"] == "pending"
    model.chat_json.assert_not_called()


def test_cache_reuse_and_source_change():
    from reportagent.services.reproduction import prepare_reproduction

    p = reviewed_paper()
    model = SimpleNamespace(chat_json=AsyncMock(return_value=output()))
    card = asyncio.run(prepare_reproduction(p, llm=model))
    p["analysis_json"] = json.dumps({"reproduction_preparation": card})
    assert asyncio.run(prepare_reproduction(p, llm=model)) == card
    assert model.chat_json.await_count == 1
    p["full_text"] += "revision changed"
    changed = asyncio.run(prepare_reproduction(p, llm=model))
    assert (
        changed["status"] == "ready" and changed["fingerprint"] != card["fingerprint"]
    )
    assert model.chat_json.await_count == 2


def test_missing_material_is_not_filled_with_invented_facts():
    from reportagent.services.reproduction import prepare_reproduction

    data = output()
    data["fields"]["training_objective"] = {
        "status": "needs_verification",
        "value": "需核对优化目标",
        "evidence": [],
    }
    card = asyncio.run(
        prepare_reproduction(
            reviewed_paper(),
            llm=SimpleNamespace(chat_json=AsyncMock(return_value=data)),
        )
    )
    assert card["status"] == "ready"
    assert "training_objective" in card["open_questions"]


def test_theory_card_accepts_omitted_empty_evidence_for_inapplicable_fields():
    from reportagent.services.reproduction import prepare_reproduction

    data = output()
    for name in ("training_objective", "data", "split"):
        data["fields"][name] = {
            "status": "not_applicable",
            "value": "理论研究不涉及此设置。",
        }
    card = asyncio.run(
        prepare_reproduction(
            reviewed_paper(),
            llm=SimpleNamespace(chat_json=AsyncMock(return_value=data)),
        )
    )
    assert card["status"] == "ready"
    assert card["fields"]["split"]["evidence"] == []


def test_omitted_evidence_cannot_make_documented_facts_valid():
    from reportagent.services.reproduction import prepare_reproduction

    data = output()
    del data["fields"]["target"]["evidence"]
    card = asyncio.run(
        prepare_reproduction(
            reviewed_paper(),
            llm=SimpleNamespace(chat_json=AsyncMock(return_value=data)),
        )
    )
    assert card["status"] == "retryable_error"
    assert not card.get("fields")


def test_model_initialization_failure_is_retryable(monkeypatch):
    from reportagent.services.reproduction import prepare_reproduction
    import reportagent.llm.client as models

    def broken():
        raise ValueError("missing configuration")

    monkeypatch.setattr(models, "LLMClient", broken)
    assert (
        asyncio.run(prepare_reproduction(reviewed_paper()))["status"]
        == "retryable_error"
    )


def test_existing_full_text_can_generate_without_a_prior_quality_review():
    from reportagent.services.reproduction import prepare_reproduction

    p = reviewed_paper()
    p["evaluation_json"] = None
    model = SimpleNamespace(chat_json=AsyncMock(return_value=output()))
    result = asyncio.run(prepare_reproduction(p, llm=model))
    assert result["status"] == "ready"
    model.chat_json.assert_awaited_once()


def test_pending_quality_review_does_not_block_independent_source_extraction():
    from reportagent.services.reproduction import prepare_reproduction

    p = reviewed_paper()
    e = json.loads(p["evaluation_json"])
    e.update(status="needs_verification", validation_issues=["某条评估引文不匹配"])
    p["evaluation_json"] = json.dumps(e)
    result = asyncio.run(
        prepare_reproduction(
            p, llm=SimpleNamespace(chat_json=AsyncMock(return_value=output()))
        )
    )
    assert result["status"] == "ready"
    assert result["fields"]["target"]["evidence"][0]["verified"] is True


def test_review_change_does_not_invalidate_independent_preparation():
    from reportagent.services.reproduction import (
        prepare_reproduction,
        saved_preparation,
    )

    p = reviewed_paper()
    model = SimpleNamespace(chat_json=AsyncMock(return_value=output()))
    card = asyncio.run(prepare_reproduction(p, llm=model))
    p["analysis_json"] = json.dumps({"reproduction_preparation": card})
    p["evaluation_json"] = None
    assert saved_preparation(p) == card
    assert asyncio.run(prepare_reproduction(p, llm=model)) == card
    model.chat_json.assert_awaited_once()


def test_timeout_returns_visible_retry_notice_and_closes_owned_client(monkeypatch):
    import reportagent.services.reproduction as preparation
    import reportagent.llm.client as models

    async def slow_response(*args, **kwargs):
        await asyncio.Event().wait()

    close = AsyncMock()
    model = SimpleNamespace(
        chat_json=slow_response, _client=SimpleNamespace(close=close)
    )
    monkeypatch.setattr(models, "LLMClient", lambda: model)
    monkeypatch.setattr(preparation, "GENERATION_TIMEOUT", 0.01)
    result = asyncio.run(preparation.prepare_reproduction(reviewed_paper()))
    assert result["status"] == "retryable_error"
    assert "超时" in result["notice"]
    close.assert_awaited_once()
