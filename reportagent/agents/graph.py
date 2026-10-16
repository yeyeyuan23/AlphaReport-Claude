from __future__ import annotations

from langgraph.graph import StateGraph, END

from reportagent.agents.state import AgentState
from reportagent.agents.collection_agent import CollectionAgent
from reportagent.agents.classification_agent import ClassificationAgent
from reportagent.agents.analysis_agent import AnalysisAgent
from reportagent.agents.storage_agent import StorageAgent
from reportagent.classifiers.rule_classifier import RuleClassifier
from reportagent.classifiers.llm_classifier import LLMClassifier
from reportagent.classifiers.quant_filter import is_quant_finance
from reportagent.llm.client import LLMClient
from reportagent.sources.local_pdf import LocalPDFSource
from reportagent.sources.arxiv_source import ArxivSource
from reportagent.sources.eastmoney_source import EastMoneySource
from reportagent.sources.bigquant_source import BigQuantSource
from reportagent.utils.config import get_config


def _build_sources():
    sources = []

    if get_config("sources", "local_pdf", "enabled", default=True):
        lib_path = get_config("sources", "local_pdf", "library_path", default="data/pdf_library")
        sources.append(LocalPDFSource(lib_path))

    if get_config("sources", "arxiv", "enabled", default=True):
        rate_limit = get_config("sources", "arxiv", "rate_limit_seconds", default=3.0)
        download_pdfs = get_config("sources", "arxiv", "download_pdfs", default=True)
        sources.append(ArxivSource(rate_limit_seconds=rate_limit, download_pdfs=download_pdfs))

    if get_config("sources", "eastmoney", "enabled", default=True):
        rate_limit = get_config("sources", "eastmoney", "rate_limit_seconds", default=1.0)
        sources.append(EastMoneySource(rate_limit_seconds=rate_limit))

    if get_config("sources", "bigquant", "enabled", default=True):
        rate_limit = get_config("sources", "bigquant", "rate_limit_seconds", default=2.0)
        max_pages = get_config("sources", "bigquant", "max_pages", default=10)
        sources.append(BigQuantSource(rate_limit_seconds=rate_limit, max_listing_pages=max_pages))

    return sources


def _build_collection_agent() -> CollectionAgent:
    sources = _build_sources()
    llm_client = None
    if get_config("collection", "query_expansion_enabled", default=True):
        try:
            llm_client = LLMClient()
        except Exception:
            pass
    return CollectionAgent(sources=sources, llm_client=llm_client)


def _build_classification_agent() -> ClassificationAgent:
    rule = RuleClassifier()
    threshold = get_config("classification", "rule_confidence_threshold", default=0.7)

    llm_cls = None
    try:
        llm_client = LLMClient()
        llm_cls = LLMClassifier(llm_client)
    except Exception:
        pass

    return ClassificationAgent(
        rule_classifier=rule,
        llm_classifier=llm_cls,
        confidence_threshold=threshold,
    )


def _should_classify(state: AgentState) -> str:
    if state["raw_results"]:
        return "classify"
    return "end"


def _should_analyze(state: AgentState) -> str:
    if state.get("classified_reports"):
        return "filter"
    return "end"


def _should_analyze_after_filter(state: AgentState) -> str:
    if state.get("classified_reports"):
        return "analyze"
    return "end"


def build_collection_graph(progress_cb=None, cancel_check=None):
    collection_agent = _build_collection_agent()
    classification_agent = _build_classification_agent()
    if progress_cb:
        classification_agent.progress_cb = progress_cb
    if cancel_check:
        classification_agent.cancel_check = cancel_check

    llm_client = None
    try:
        llm_client = LLMClient()
    except Exception:
        pass
    analysis_agent = AnalysisAgent(llm_client=llm_client)
    if progress_cb:
        analysis_agent.progress_cb = progress_cb
    if cancel_check:
        analysis_agent.cancel_check = cancel_check

    storage_agent = StorageAgent()

    async def collect(state: AgentState) -> AgentState:
        return await collection_agent.run(state)

    async def classify(state: AgentState) -> AgentState:
        return await classification_agent.run(state)

    async def quant_filter(state: AgentState) -> AgentState:
        """Filter out non-quant papers before analysis and storage."""
        reports = state.get("classified_reports", [])
        if not reports:
            return state

        passed: list = []
        rejected: list[str] = []
        for cr in reports:
            sr = cr.search_result
            title = sr.title or ""
            abstract = sr.abstract or ""
            if is_quant_finance(title, abstract):
                passed.append(cr)
            else:
                rejected.append(title[:80])

        if rejected:
            state["messages"].append(
                f"Quant filter: rejected {len(rejected)} non-quant papers: "
                + "; ".join(rejected[:5])
                + (f" ... and {len(rejected)-5} more" if len(rejected) > 5 else "")
            )

        state["classified_reports"] = passed
        return state

    async def analyze(state: AgentState) -> AgentState:
        return await analysis_agent.run(state)

    async def store(state: AgentState) -> AgentState:
        return await storage_agent.run(state)

    graph = StateGraph(AgentState)

    graph.add_node("collect", collect)
    graph.add_node("classify", classify)
    graph.add_node("filter", quant_filter)
    graph.add_node("analyze", analyze)
    graph.add_node("store", store)

    graph.set_entry_point("collect")

    graph.add_conditional_edges(
        "collect",
        _should_classify,
        {
            "classify": "classify",
            "end": END,
        },
    )

    graph.add_conditional_edges(
        "classify",
        _should_analyze,
        {
            "filter": "filter",
            "end": END,
        },
    )

    graph.add_conditional_edges(
        "filter",
        _should_analyze_after_filter,
        {
            "analyze": "analyze",
            "end": END,
        },
    )

    graph.add_edge("analyze", "store")
    graph.add_edge("store", END)

    return graph.compile()
