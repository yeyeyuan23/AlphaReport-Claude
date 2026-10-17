from __future__ import annotations

import logging

from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from reportagent.skills.registry import load_all_skills, format_skills_context
from reportagent.chat.tools import ALL_TOOLS
from reportagent.utils.config import (
    PROJECT_ROOT,
    get_config,
    get_llm_api_key,
    get_llm_base_url,
    get_llm_model,
)

logger = logging.getLogger(__name__)


def _load_system_prompt() -> str:
    rel = get_config(
        "chat", "system_prompt_path", default="configs/prompts/chat_system.txt"
    )
    path = PROJECT_ROOT / rel
    if path.exists():
        return path.read_text(encoding="utf-8").strip() + format_skills_context(
            load_all_skills()
        )
    return (
        "你是 ResearchAgent 智能助手，专注于量化金融研报的收集和分析。"
        + format_skills_context(load_all_skills())
    )


def _build_llm() -> ChatOpenAI:
    kwargs = {
        "model": get_llm_model(),
        "api_key": get_llm_api_key(),
        "streaming": True,
        "temperature": 0.3,
        "max_tokens": 4000,
    }
    base_url = get_llm_base_url()
    if base_url:
        kwargs["base_url"] = base_url
    return ChatOpenAI(**kwargs)


def build_chat_agent():
    llm = _build_llm()
    system_prompt = _load_system_prompt()

    return create_react_agent(
        llm,
        ALL_TOOLS,
        prompt=system_prompt,
    )


def build_chat_agent_v2():
    """Build the Plan-Execute-Reflect agent (replaces simple ReAct loop).

    This agent plans subtasks before executing, reflects on intermediate
    results, and synthesises a coherent answer across multiple tool calls.
    Use this when the user request requires multi-step reasoning.
    """
    from reportagent.chat.graph import build_plan_execute_graph

    return build_plan_execute_graph()
