"""JSON responses remain available for diagnosis without recording credentials."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reportagent.llm.client import LLMClient
from reportagent.llm import diagnostics


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_default_output_budget_reaches_provider_request(monkeypatch, provider):
    import copy
    from reportagent.utils import config

    settings = copy.deepcopy(config.load_config())
    settings["llm"]["default_max_output_tokens"] = 987
    monkeypatch.setattr(config, "load_config", lambda: settings)
    requests = []

    async def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(
            model="fixture",
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="Complete answer")],
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="Complete answer"),
                )
            ],
            usage=SimpleNamespace(
                input_tokens=1, output_tokens=1, prompt_tokens=1, completion_tokens=1
            ),
        )

    client = LLMClient.__new__(LLMClient)
    client.provider, client.model, client.thinking = provider, "fixture", None
    client.calls = []
    client._client = SimpleNamespace(
        messages=SimpleNamespace(create=create),
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    assert (
        asyncio.run(client.chat([{"role": "user", "content": "Read"}]))
        == "Complete answer"
    )
    assert requests[0]["max_tokens"] == 987


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
@pytest.mark.parametrize("valid", [True, False])
def test_full_response_and_exact_parser_input_are_saved(
    tmp_path, monkeypatch, provider, valid
):
    monkeypatch.setattr(diagnostics, "RESPONSE_ROOT", tmp_path / "data/model_responses")
    content = json.dumps({"finding": "evidence " * 300, "value": 1})
    if not valid:
        content = content[:-1] + ", invalid}"
    raw = "```json\n" + content + "\n```"
    body = {"id": "model-response", "model": "fixture", "content": raw}
    response = SimpleNamespace(
        model="fixture",
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=raw)],
        choices=[
            SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=raw))
        ],
        usage=SimpleNamespace(
            input_tokens=1, output_tokens=2, prompt_tokens=1, completion_tokens=2
        ),
        model_dump=lambda **kwargs: body,
    )
    create = AsyncMock(return_value=response)
    client = LLMClient.__new__(LLMClient)
    client.provider, client.model, client.thinking = provider, "fixture", None
    client._api_key = "must-not-appear-in-diagnostics"
    client.calls = []
    client.diagnostic_context = {"task": "full_text_review", "report_id": 108}
    client._client = SimpleNamespace(
        messages=SimpleNamespace(create=create),
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    if valid:
        result = asyncio.run(
            client.chat_json([{"role": "user", "content": "Read the paper"}])
        )
        assert result["value"] == 1
    else:
        with pytest.raises(json.JSONDecodeError) as caught:
            asyncio.run(
                client.chat_json([{"role": "user", "content": "Read the paper"}])
            )
    files = list((tmp_path / "data/model_responses").rglob("response.txt"))
    assert len(files) == 1
    folder = files[0].parent
    assert files[0].read_text() == raw
    assert json.loads((folder / "response.json").read_text()) == body
    metadata = json.loads((folder / "metadata.json").read_text())
    assert metadata["report_id"] == 108 and metadata["task"] == "full_text_review"
    if not valid:
        error = json.loads((folder / "parse-error.json").read_text())
        assert error["message"] == caught.value.msg
        assert (error["line"], error["column"], error["position"]) == (
            caught.value.lineno,
            caught.value.colno,
            caught.value.pos,
        )
        assert (folder / "parse-input.txt").read_text() == caught.value.doc
        assert client.calls[-1]["response_path"] == str(folder)
    for file in folder.iterdir():
        assert client._api_key not in file.read_text()
    create.assert_awaited_once()


def test_diagnostic_write_failure_does_not_hide_the_parser_error(monkeypatch):
    def cannot_write(*args, **kwargs):
        raise OSError("offline read-only diagnostics directory")

    monkeypatch.setattr(diagnostics, "save_response", cannot_write)
    client = LLMClient.__new__(LLMClient)
    client.provider, client.model, client.calls = "anthropic", "fixture", []
    with pytest.raises(json.JSONDecodeError) as caught:
        client._parse_json_response('{"broken": }')
    assert caught.value.msg == "Expecting value"


@pytest.mark.parametrize("invalid", ["field", "judgment"])
def test_review_validation_preserves_exact_errors_and_input(
    tmp_path, monkeypatch, caplog, invalid
):
    from pydantic import ValidationError
    from tests.support.report_fixtures import Model, module, paper, response

    data = response()
    if invalid == "field":
        data["research_type"] = "unsupported-type"
    else:
        data["quality_checks"][0]["status"] = "limited"
    raw = json.dumps(data, ensure_ascii=False)
    monkeypatch.setattr(diagnostics, "RESPONSE_ROOT", tmp_path)
    directory = diagnostics.save_response(
        raw, None, provider="anthropic", call={}, context={"report_id": 89}
    )
    llm = Model(data)
    llm.calls = [{"response_path": str(directory)}]
    with pytest.raises(ValidationError) as caught:
        asyncio.run(module().evaluate_paper(paper(id=89), llm))

    saved = json.loads((directory / "validation-error.json").read_text())
    assert saved["errors"] == json.loads(caught.value.json(include_url=False))
    assert saved["schema"] == "Review"
    assert json.loads((directory / "validation-input.json").read_text()) == data
    assert (directory / "response.txt").read_text() == raw
    assert "response_path=" + str(directory) in caplog.text
    assert caught.value.errors()[0]["msg"] in caplog.text
