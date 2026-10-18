"""Only explicit provider context errors classify an input as too large."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from anthropic import BadRequestError, RateLimitError

from reportagent.llm.client import LLMClient
from reportagent.llm.errors import ContextWindowExceeded


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize(
    "failure", ["context", "context_code", "bad_format", "rate_limit", "timeout"]
)
def test_provider_errors_preserve_distinct_causes_and_safe_call_receipts(
    provider, failure
):
    status = 429 if failure == "rate_limit" else 400
    message = (
        "This model's maximum context length is 1000 tokens"
        if failure in {"context", "rate_limit"}
        else "Invalid response_format"
    )
    # Both transports normalize status/body attributes, not SDK exception classes.
    error_class = RateLimitError if failure == "rate_limit" else BadRequestError
    response = httpx.Response(
        status, request=httpx.Request("POST", "https://model.test")
    )
    details = {"message": message}
    if failure == "context_code":
        details["code"] = "context_length_exceeded"
    error = (
        httpx.ReadTimeout("offline timeout")
        if failure == "timeout"
        else error_class(message, response=response, body={"error": details})
    )
    create = AsyncMock(side_effect=error)
    client = LLMClient.__new__(LLMClient)
    client.provider, client.model, client.thinking = provider, "configured-model", None
    client.calls = []
    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        messages=SimpleNamespace(create=create),
    )
    expected = ContextWindowExceeded if failure.startswith("context") else type(error)
    with pytest.raises(expected) as caught:
        asyncio.run(client.chat_json([{"role": "user", "content": "full evidence"}]))
    assert create.await_count == 1
    assert client.calls[0]["complete"] is False
    if failure.startswith("context"):
        assert client.calls[0]["error"] == "context_length_exceeded"
        assert "configured-model" in str(caught.value)
    else:
        assert caught.value is error
        assert "error" not in client.calls[0]
