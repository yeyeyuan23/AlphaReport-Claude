"""Plan-Execute-Reflect Agent — replaces the basic ReAct loop.

Flow:  START → plan → execute → reflect → [loop or finish] → respond → END

The planner breaks the user's request into subtasks, the executor runs tool
calls for each step, the reflector checks whether results are sufficient (and
may trigger replanning), and the responder synthesises a final answer.
"""

from __future__ import annotations

import logging
from typing import Literal

from langgraph.graph import StateGraph, END
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_core.tools import BaseTool

from reportagent.chat.tools import ALL_TOOLS
from reportagent.skills.registry import format_skills_context, load_all_skills
from reportagent.utils.config import get_config, PROJECT_ROOT

logger = logging.getLogger(__name__)

# ── State ────────────────────────────────────────────────────────────


class AgentState(dict):
    """Mutable agent state shared across graph nodes."""

    pass


def init_state(messages: list, max_iterations: int | None = None) -> dict:
    max_iter = max_iterations or get_config("chat", "max_tool_iterations", default=10)
    return {
        "messages": messages,
        "plan": [],
        "step_index": 0,
        "iteration": 0,
        "max_iterations": max_iter,
        "final_response": "",
        "reflection_notes": [],
    }


def _load_system_prompt() -> str:
    """Load the chat system prompt for the current agent version."""
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


# ── Tool registry ────────────────────────────────────────────────────

_TOOLS_BY_NAME: dict[str, BaseTool] = {t.name: t for t in ALL_TOOLS}


def _format_tools_for_llm() -> str:
    """Return a compact tool manifest with parameter names for the planner LLM."""
    lines = []
    for t in ALL_TOOLS:
        desc = t.description.split("\n")[0] if t.description else ""
        sig_parts = []
        if hasattr(t, "args_schema") and t.args_schema:
            try:
                schema = t.args_schema.model_json_schema()
                for prop, info in schema.get("properties", {}).items():
                    sig_parts.append(prop)
            except Exception:
                pass
        sig = ", ".join(sig_parts)
        lines.append(f"- {t.name}({sig}): {desc}")
    return "\n".join(lines)


# ── Planner ──────────────────────────────────────────────────────────


def _plan_prompt(
    user_query: str,
    tool_manifest: str,
    history_summary: str = "",
    system_prompt: str = "",
) -> str:
    return f"""{system_prompt}

根据用户请求，制定一个多步骤执行计划。

可用工具：
{tool_manifest}

{history_summary}

用户请求：{user_query}

请制定执行计划。每个步骤需要调用一个工具。

【参数填写规则 — 非常重要】
- 对于独立参数（搜索关键词、主题、分析类型等），直接填入具体值
- 对于依赖前一步结果的参数（如 report_id），填 null，系统会自动从前一步结果中提取
- 搜索类工具如果没有找到结果，后续步骤会自动跳过
- 不要填"从前一步获取"之类的占位文本，直接填 null

【计划要求】
- 步骤顺序合理：先搜索→获取→分析→对比
- 复杂请求拆成 3-6 步，简单请求 1-2 步
- 每步只做一件事

返回 JSON（只返回 JSON，不要其他文字）：
```json
{{"plan": [
  {{"step": 1, "description": "步骤描述", "tool": "工具名", "arguments": {{"arg1": "value1"}}, "reason": "为什么"}}
]}}
```"""


async def _llm_plan(messages: list) -> list[dict]:
    """Call LLM to generate a step-by-step plan."""
    from reportagent.llm.client import LLMClient

    user_query = ""
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            user_query = m.content
            break
        elif isinstance(m, dict) and m.get("role") == "user":
            user_query = m["content"]
            break

    if not user_query:
        user_query = str(
            messages[-1].content if hasattr(messages[-1], "content") else messages[-1]
        )

    tool_manifest = _format_tools_for_llm()

    history_summary = ""
    if len(messages) > 2:
        # Include SystemMessages (report context) and recent conversation
        recent = messages[-8:]
        if len(recent) > 1:
            parts = []
            for m in recent[:-1]:
                if not hasattr(m, "content") or not m.content:
                    continue
                content = m.content
                # Keep SystemMessages intact (they contain report context)
                if isinstance(m, SystemMessage):
                    parts.append(f"[上下文] {content}")
                else:
                    # Show more of recent assistant messages (which contain report refs)
                    limit = 600 if isinstance(m, AIMessage) else 300
                    parts.append(content[:limit])
            if parts:
                history_summary = "对话历史：\n" + "\n---\n".join(parts)

    system_prompt = _load_system_prompt()
    prompt = _plan_prompt(user_query, tool_manifest, history_summary, system_prompt)

    client = LLMClient()
    try:
        resp = await client.chat_json(
            [{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=1500,
        )
        return resp.get("plan", [])
    except Exception as e:
        logger.warning("Planner LLM failed: %s", e)
        # Fallback: single-step plan using chat directly
        return [
            {
                "step": 1,
                "description": "直接回答",
                "tool": "none",
                "arguments": {},
                "reason": "规划失败，直接回复",
            }
        ]


# ── Executor ─────────────────────────────────────────────────────────


def _resolve_dependent_args(
    args: dict, plan: list[dict], current_step_idx: int
) -> dict:
    """Fill in null arguments by extracting values from previous step results."""
    resolved = dict(args)
    previous_steps = plan[:current_step_idx]
    previous = previous_steps[-1] if previous_steps else {}
    prev_results = (
        [previous.get("result", "")] if previous.get("status") == "done" else []
    )

    for key, val in resolved.items():
        if val is not None and str(val).strip():
            continue

        # Try to extract report_id from previous results
        if key == "report_id" and prev_results:
            import re

            # Only the immediate prerequisite may supply an ID. An empty new
            # search must not resurrect a result from an earlier search.
            ids = {
                int(value)
                for value in re.findall(r"\[(?:ID:\s*)?(\d+)\]", str(prev_results[-1]))
                if int(value) > 0
            }
            if len(ids) == 1:
                resolved[key] = ids.pop()
            elif len(ids) > 1:
                raise ValueError(
                    "report_id is ambiguous; choose a specific report before continuing"
                )
        if key == "report_id" and (
            not isinstance(resolved[key], int)
            or isinstance(resolved[key], bool)
            or resolved[key] <= 0
        ):
            raise ValueError(
                "report_id could not be resolved; search returned no unique report"
            )

    return resolved


async def _execute_step(step: dict, plan: list[dict]) -> dict:
    """Execute a single plan step by calling the specified tool."""
    tool_name = step.get("tool", "none")
    args = step.get("arguments", {})

    if tool_name == "none":
        return {"step": step["step"], "result": "跳过（无需工具）", "status": "skipped"}

    step_idx = step.get("step", 0) - 1  # step numbers are 1-based
    try:
        args = _resolve_dependent_args(args, plan, step_idx)
    except ValueError as error:
        return {"step": step["step"], "result": str(error), "status": "error"}

    tool = _TOOLS_BY_NAME.get(tool_name)
    if not tool:
        return {
            "step": step["step"],
            "result": f"工具 '{tool_name}' 不存在",
            "status": "error",
        }

    try:
        if hasattr(tool, "ainvoke"):
            result = await tool.ainvoke(args)
        else:
            result = tool.invoke(args)
        result_str = str(result) if not isinstance(result, str) else result
        return {
            "step": step["step"],
            "result": result_str[:4000],
            "status": "done",
        }
    except Exception as e:
        logger.warning("Tool %s failed: %s", tool_name, e)
        return {
            "step": step["step"],
            "result": f"执行失败: {e}",
            "status": "error",
        }


# ── Reflector ────────────────────────────────────────────────────────


_REFLECT_PROMPT = """你是量化金融研究助手，正在执行一个多步骤任务。请评估当前执行结果。

原始用户请求：{user_query}

执行计划：
{plan_summary}

当前已完成步骤的结果：
{step_results}

请评估：
1. 已完成步骤是否提供了足够的信息来回答用户的问题？
2. 是否需要调整后续步骤？（例如：搜索结果太少需要扩大范围，结果质量差需要换数据源）
3. 是否可以进入回答阶段？

返回 JSON（只返回 JSON）：
```json
{{"decision": "continue | replan | respond",
 "reason": "简短理由",
 "replan_suggestions": "如果需要重新规划，建议修改什么"
}}```"""


async def _llm_reflect(state: dict, user_query: str) -> dict:
    """LLM-based reflection on execution progress."""
    from reportagent.llm.client import LLMClient

    plan = state.get("plan", [])

    # Summarize completed steps
    executed = [s for s in plan if s.get("status") in ("done", "error", "skipped")]
    pending = [s for s in plan if s.get("status") not in ("done", "error", "skipped")]

    plan_summary = "\n".join(
        f"  步骤{s['step']}: {s.get('description', '')} [工具: {s.get('tool', '')}] → {s.get('status', 'pending')}"
        for s in plan
    )
    step_results = "\n".join(
        f"  步骤{s['step']} ({s.get('tool', '')}): {s.get('result', '')[:300]}"
        for s in executed
    )

    if not executed:
        return {"decision": "continue", "reason": "尚未执行任何步骤"}

    # Simple heuristic: if all steps done, respond
    if not pending:
        return {"decision": "respond", "reason": "所有步骤已完成"}

    # LLM-based reflection for complex cases
    if len(plan) >= 3:
        try:
            client = LLMClient()
            prompt = _REFLECT_PROMPT.format(
                user_query=user_query,
                plan_summary=plan_summary,
                step_results=step_results,
            )
            resp = await client.chat_json(
                [{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=500,
            )
            return {
                "decision": resp.get("decision", "continue"),
                "reason": resp.get("reason", ""),
                "replan_suggestions": resp.get("replan_suggestions", ""),
            }
        except Exception as e:
            logger.warning("Reflector LLM failed: %s", e)

    # Fallback: continue if steps pending
    return {"decision": "continue", "reason": "还有步骤待执行"}


# ── Responder ────────────────────────────────────────────────────────


_RESPOND_PROMPT = """{system_prompt}

根据以下执行结果，用中文回答用户的问题。

用户请求：{user_query}

执行步骤和结果：
{results_summary}

要求：
- 综合所有步骤的结果，给出连贯、有条理的回答
- 引用具体的数据、论文标题、分析方法
- 如果有未覆盖的方面，诚实说明
- 如果用户要求对比分析，用表格呈现
- 使用 Markdown 格式化输出
- 所有数学符号用 $...$ 格式"""


async def _llm_respond(state: dict, user_query: str) -> str:
    """Generate final response from all step results."""
    from reportagent.llm.client import LLMClient

    plan = state.get("plan", [])
    results_summary = "\n\n".join(
        f"### 步骤{s['step']}: {s.get('description', '')}\n"
        f"工具: {s.get('tool', '')}\n"
        f"结果: {s.get('result', '')[:2500]}"
        for s in plan
        if s.get("status") in ("done", "error", "skipped")
    )

    system_prompt = _load_system_prompt()

    client = LLMClient()
    try:
        prompt = _RESPOND_PROMPT.format(
            system_prompt=system_prompt,
            user_query=user_query,
            results_summary=results_summary,
        )
        resp = await client.chat(
            [{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=3000,
        )
        return resp
    except Exception as e:
        logger.warning("Responder LLM failed: %s", e)
        # Fallback: concatenate raw results
        return "\n\n".join(
            f"## 步骤{s['step']}\n{s.get('result', '')}"
            for s in plan
            if s.get("status") == "done"
        )


# ── Graph Nodes ──────────────────────────────────────────────────────


async def _plan_node(state: dict) -> dict:
    logger.debug("Plan node: generating plan")
    messages = state.get("messages", [])

    # Extract user query
    user_query = ""
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            user_query = m.content
            break
        elif isinstance(m, dict) and m.get("role") == "user":
            user_query = m["content"]
            break
    if not user_query:
        user_query = str(messages[-1])

    plan = await _llm_plan(messages)
    state["plan"] = plan
    state["step_index"] = 0
    state["iteration"] = 0
    state["reflection_notes"] = []

    logger.debug("Plan generated: %d steps", len(plan))
    return state


async def _execute_node(state: dict) -> dict:
    plan = state.get("plan", [])
    step_idx = state.get("step_index", 0)

    if step_idx >= len(plan):
        return state

    step = plan[step_idx]
    logger.debug(
        "Execute node: step %d/%d (%s)", step_idx + 1, len(plan), step.get("tool", "?")
    )

    result = await _execute_step(step, plan)
    plan[step_idx].update(result)
    state["step_index"] = step_idx + 1
    state["iteration"] = state.get("iteration", 0) + 1

    return state


async def _reflect_node(state: dict) -> dict:
    messages = state.get("messages", [])
    user_query = ""
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            user_query = m.content
            break
        elif isinstance(m, dict) and m.get("role") == "user":
            user_query = m["content"]
            break

    reflection = await _llm_reflect(state, user_query)
    state["reflection_notes"].append(reflection.get("reason", ""))
    logger.debug("Reflect node: decision=%s", reflection.get("decision"))

    # Store decision for routing
    state["_reflect_decision"] = reflection.get("decision", "continue")
    return state


async def _respond_node(state: dict) -> dict:
    messages = state.get("messages", [])
    user_query = ""
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            user_query = m.content
            break
        elif isinstance(m, dict) and m.get("role") == "user":
            user_query = m["content"]
            break

    logger.debug("Respond node: generating final answer")
    response = await _llm_respond(state, user_query)
    state["final_response"] = response
    return state


# ── Routing ──────────────────────────────────────────────────────────


def _route_after_plan(state: dict) -> Literal["execute", "respond"]:
    plan = state.get("plan", [])
    if not plan:
        return "respond"
    return "execute"


def _route_after_reflect(state: dict) -> Literal["execute", "respond"]:
    decision = state.get("_reflect_decision", "continue")
    iteration = state.get("iteration", 0)
    max_iter = state.get("max_iterations", 10)
    plan = state.get("plan", [])
    step_idx = state.get("step_index", 0)

    if decision == "respond":
        return "respond"
    if step_idx >= len(plan):
        return "respond"
    if iteration >= max_iter:
        logger.warning("Max iterations reached (%d)", max_iter)
        return "respond"
    return "execute"


# ── Build ────────────────────────────────────────────────────────────


def build_plan_execute_graph():
    """Build the Plan-Execute-Reflect agent graph."""

    graph = StateGraph(dict)

    graph.add_node("plan", _plan_node)
    graph.add_node("execute", _execute_node)
    graph.add_node("reflect", _reflect_node)
    graph.add_node("respond", _respond_node)

    graph.set_entry_point("plan")

    graph.add_conditional_edges(
        "plan",
        _route_after_plan,
        {"execute": "execute", "respond": "respond"},
    )

    graph.add_edge("execute", "reflect")

    graph.add_conditional_edges(
        "reflect",
        _route_after_reflect,
        {"execute": "execute", "respond": "respond"},
    )

    graph.add_edge("respond", END)

    return graph.compile()


# ── Streaming wrapper ────────────────────────────────────────────────


async def run_with_stream(
    messages: list,
    config: dict | None = None,
):
    """Run the Plan-Execute agent and yield SSE events.

    Runs the full graph first, then emits structured events from the final state.
    This is reliable — the frontend receives plan → tool_results → response in order.
    """
    graph = build_plan_execute_graph()
    state = init_state(messages)

    yield {"type": "phase", "phase": "planning", "content": "制定执行计划..."}

    # Run the full Plan-Execute-Reflect-Respond pipeline
    try:
        final_state = await graph.ainvoke(state, config if config else {})
    except Exception as e:
        logger.exception("Graph execution failed")
        yield {"type": "error", "content": str(e)}
        return

    plan = final_state.get("plan", [])

    # Emit the plan
    yield {
        "type": "plan",
        "steps": [
            {
                "step": s.get("step"),
                "description": s.get("description"),
                "tool": s.get("tool"),
            }
            for s in plan
        ],
    }

    # Emit each executed step
    yield {"type": "phase", "phase": "executing", "content": "执行中..."}
    for s in plan:
        status = s.get("status", "pending")
        if status in ("done", "error", "skipped"):
            yield {
                "type": "tool_call",
                "name": s.get("tool", ""),
                "args": s.get("arguments", {}),
            }
            result_text = s.get("result", "")[:2000]
            yield {
                "type": "tool_result",
                "name": s.get("tool", ""),
                "result": result_text,
            }

    # Emit the response
    yield {"type": "phase", "phase": "responding", "content": "生成回答..."}
    response = final_state.get("final_response", "")
    if response:
        for i in range(0, len(response), 30):
            yield {"type": "token", "content": response[i : i + 30]}

    yield {
        "type": "done",
        "iterations": final_state.get("iteration", 0),
        "steps_completed": sum(1 for s in plan if s.get("status") == "done"),
    }
