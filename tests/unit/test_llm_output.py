import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from reportagent.llm.client import LLMClient


class LLMOutputTests(unittest.IsolatedAsyncioTestCase):
    def client(self, reason="end_turn", text="ok"):
        client = LLMClient.__new__(LLMClient)
        client.model = "deepseek-v4-pro"
        client.thinking = "disabled"
        client.calls = []
        client._client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(
            model="deepseek-v4-pro", stop_reason=reason,
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=10, output_tokens=20),
        ))))
        return client

    async def test_explicit_thinking_and_complete_response_recorded(self):
        client = self.client()
        self.assertEqual(await client._chat_anthropic([{"role": "user", "content": "test"}], .1, 100), "ok")
        self.assertEqual(client._client.messages.create.call_args.kwargs["thinking"], {"type": "disabled"})
        self.assertTrue(client.calls[0]["complete"])

    async def test_truncated_or_empty_response_is_rejected(self):
        for reason, text in [("max_tokens", "partial"), ("end_turn", "")]:
            client = self.client(reason, text)
            with self.assertRaises(ValueError):
                await client._chat_anthropic([{"role": "user", "content": "test"}], .1, 100)
            self.assertFalse(client.calls[0]["complete"])

    async def test_configured_thinking_policy_applies_to_default_client(self):
        from unittest.mock import patch
        response_client = self.client()._client
        with patch('reportagent.utils.config.load_config', return_value={'llm': {'thinking': 'disabled'}}), patch.object(
            LLMClient, '_build_anthropic_client', return_value=response_client
        ):
            client = LLMClient(provider='anthropic', api_key='fixture', model='fixture', base_url='https://fixture')
            await client.chat([{'role': 'user', 'content': 'classify'}], max_tokens=2000)
        self.assertEqual(response_client.messages.create.call_args.kwargs['thinking'], {'type': 'disabled'})
