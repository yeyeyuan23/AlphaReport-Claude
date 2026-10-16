from __future__ import annotations

import json
import logging
from typing import Any

from reportagent.utils.config import (
    get_config,
    get_llm_api_key,
    get_llm_base_url,
    get_llm_model,
    get_llm_provider,
)

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(
        self,
        provider: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        thinking: str | None = None,
    ):
        self.provider = provider or get_llm_provider()
        self.model = model or get_llm_model()
        self._api_key = api_key or get_llm_api_key()
        self._base_url = base_url or get_llm_base_url()
        self.thinking = thinking if thinking is not None else get_config("llm", "thinking")
        self.calls: list[dict] = []

        if self.provider == "anthropic":
            self._client = self._build_anthropic_client()
        else:
            self._client = self._build_openai_client()

    def _build_openai_client(self):
        from openai import AsyncOpenAI
        kwargs: dict[str, Any] = {"api_key": self._api_key}
        if self._base_url:
            kwargs["base_url"] = self._base_url
        return AsyncOpenAI(**kwargs)

    def _build_anthropic_client(self):
        from anthropic import AsyncAnthropic
        kwargs: dict[str, Any] = {"api_key": self._api_key}
        if self._base_url:
            kwargs["base_url"] = self._base_url
        return AsyncAnthropic(**kwargs)

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 2000,
        model: str | None = None,
    ) -> str:
        if self.provider == "anthropic":
            return await self._chat_anthropic(messages, temperature, max_tokens, model=model)
        return await self._chat_openai(messages, temperature, max_tokens, model=model)

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 2000,
    ):
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        async for chunk in response:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.1,
        max_tokens: int = 2000,
        model: str | None = None,
    ) -> dict[str, Any]:
        if self.provider == "anthropic":
            return await self._chat_json_anthropic(messages, temperature, max_tokens, model=model)
        return await self._chat_json_openai(messages, temperature, max_tokens, model=model)

    @staticmethod
    def _safe_json_parse(text: str) -> dict[str, Any]:
        """Parse JSON from LLM output, handling common formatting issues.

        Handles: (1) markdown fences, (2) unescaped LaTeX backslashes,
        (3) reasoning-model prefixes.
        """
        # Strip markdown fences
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # Fix unescaped LaTeX backslashes (e.g. \tau, \mathbb).
        # Use character-by-character scanning because the raw text may mix
        # properly escaped \\ with single \ that need fixing.
        # Valid JSON escapes: \", \\, \/, \b, \f, \n, \r, \t, \uXXXX
        chars: list[str] = []
        i = 0
        while i < len(cleaned):
            c = cleaned[i]
            if c == "\\" and i + 1 < len(cleaned):
                nxt = cleaned[i + 1]
                if nxt in '"\\/bfnrtu':
                    chars.append("\\")
                    chars.append(nxt)
                    i += 2
                elif nxt == "u" and i + 5 < len(cleaned) and all(
                    h in "0123456789abcdefABCDEF" for h in cleaned[i + 2 : i + 6]
                ):
                    chars.append("\\")
                    chars.append(cleaned[i + 1 : i + 6])
                    i += 6
                else:
                    chars.append("\\\\")
                    i += 1
            else:
                chars.append(c)
                i += 1
        try:
            return json.loads("".join(chars))
        except json.JSONDecodeError:
            import logging
            logging.getLogger(__name__).warning(
                "_safe_json_parse still failed after char-level fix. Raw (first 500): %s",
                repr(cleaned[:500]),
            )
            raise

    # --- OpenAI ---

    async def _chat_openai(
        self, messages: list[dict[str, str]], temperature: float, max_tokens: int,
        model: str | None = None,
    ) -> str:
        return await self._openai_text({
            "model": model or self.model, "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens,
        })

    async def _chat_json_openai(
        self, messages: list[dict[str, str]], temperature: float, max_tokens: int,
        model: str | None = None,
    ) -> dict[str, Any]:
        content = await self._openai_text({
            "model": model or self.model, "messages": self._inject_json_instruction(messages),
            "temperature": temperature, "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        })
        return self._safe_json_parse(content)

    async def _openai_text(self, kwargs: dict) -> str:
        try:
            response = await self._client.chat.completions.create(**kwargs)
        except Exception:
            self.calls.append({"model": kwargs["model"], "complete": False})
            raise
        choice = response.choices[0]
        content = choice.message.content or ""
        complete = choice.finish_reason == "stop" and bool(content.strip())
        self.calls.append({"model": response.model, "stop_reason": choice.finish_reason,
                           "input_tokens": response.usage.prompt_tokens,
                           "output_tokens": response.usage.completion_tokens, "complete": complete})
        if not complete:
            raise ValueError(f"Incomplete model response: {choice.finish_reason}")
        return content

    # --- Anthropic / Claude ---

    @staticmethod
    def _extract_text(content: list) -> str:
        """Extract text from Anthropic response, skipping thinking blocks."""
        for block in content:
            if hasattr(block, "text") and getattr(block, "type", None) == "text":
                return block.text
        # Fallback: try any block with .text
        for block in content:
            if hasattr(block, "text"):
                return block.text
        return ""

    async def _chat_anthropic(
        self, messages: list[dict[str, str]], temperature: float, max_tokens: int,
        model: str | None = None,
    ) -> str:
        system, user_messages = self._split_system_message(messages)
        kwargs: dict[str, Any] = {
            "model": model or self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": user_messages,
        }
        if system:
            kwargs["system"] = system
        return await self._anthropic_text(kwargs)

    async def _anthropic_text(self, kwargs: dict) -> str:
        if self.thinking is not None:
            kwargs["thinking"] = {"type": self.thinking}
        try:
            response = await self._client.messages.create(**kwargs)
        except Exception:
            self.calls.append({"model": kwargs["model"], "complete": False})
            raise
        text = self._extract_text(response.content)
        complete = response.stop_reason in ("end_turn", "stop_sequence") and bool(text.strip())
        self.calls.append({
            "model": response.model, "stop_reason": response.stop_reason,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens, "complete": complete,
        })
        logger.info("Model response: model=%s stop=%s output_tokens=%s", response.model,
                    response.stop_reason, response.usage.output_tokens)
        if not complete:
            raise ValueError(f"Incomplete model response: {response.stop_reason}")
        return text

    async def _chat_json_anthropic(
        self, messages: list[dict[str, str]], temperature: float, max_tokens: int,
        model: str | None = None,
    ) -> dict[str, Any]:
        patched = self._inject_json_instruction(messages)
        system, user_messages = self._split_system_message(patched)
        kwargs: dict[str, Any] = {
            "model": model or self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": user_messages,
        }
        if system:
            kwargs["system"] = system
        text = await self._anthropic_text(kwargs)
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        return self._safe_json_parse(text)

    @staticmethod
    def _split_system_message(
        messages: list[dict[str, str]],
    ) -> tuple[str, list[dict[str, str]]]:
        system_parts = []
        user_messages = []
        for m in messages:
            if m["role"] == "system":
                system_parts.append(m["content"])
            else:
                user_messages.append(m)
        return "\n\n".join(system_parts), user_messages

    @staticmethod
    def _inject_json_instruction(
        messages: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        patched = list(messages)
        if patched and patched[-1]["role"] == "user":
            patched[-1] = {
                **patched[-1],
                "content": patched[-1]["content"]
                + "\n\nIMPORTANT: Respond with ONLY a valid JSON object, no markdown fences or extra text.",
            }
        return patched

    # --- Shared ---

    async def expand_queries(self, topics: list[str], keywords: list[str]) -> list[str]:
        prompt = (
            "Given these research topics and keywords, generate 3-5 expanded search queries "
            "that would help find relevant academic papers and research reports. "
            "Include both English and Chinese variants where applicable.\n\n"
            f"Topics: {', '.join(topics)}\n"
            f"Keywords: {', '.join(keywords)}\n\n"
            "Return a JSON object with key 'queries' containing a list of search query strings."
        )
        result = await self.chat_json([{"role": "user", "content": prompt}])
        return result.get("queries", topics)
