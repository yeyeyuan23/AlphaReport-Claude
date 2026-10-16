from __future__ import annotations

import logging

from reportagent.agents.state import AgentState
from reportagent.classifiers.rule_classifier import RuleClassifier
from reportagent.classifiers.llm_classifier import LLMClassifier
from reportagent.models.schemas import ClassifiedReport, ClassificationResult

logger = logging.getLogger(__name__)


class ClassificationAgent:
    def __init__(
        self,
        rule_classifier: RuleClassifier,
        llm_classifier: LLMClassifier | None = None,
        confidence_threshold: float = 0.7,
    ):
        self.rule_classifier = rule_classifier
        self.llm_classifier = llm_classifier
        self.confidence_threshold = confidence_threshold
        self.progress_cb = None
        self.cancel_check = None

    async def run(self, state: AgentState) -> AgentState:
        state["classification_status"] = "classifying"
        state["current_phase"] = "classifying"
        total = len(state["raw_results"])
        state["messages"].append(f"Classifying {total} reports...")
        if self.progress_cb:
            self.progress_cb("classifying", f"Classifying {total} reports...")

        classified = []
        llm_used = 0
        for i, sr in enumerate(state["raw_results"]):
            if self.cancel_check and self.cancel_check():
                state["classification_status"] = "cancelled"
                state["messages"].append("Classification cancelled by user")
                return state

            text = sr.abstract or sr.full_text or ""
            title = sr.title or ""

            rule_result = self.rule_classifier.classify(title, text)

            if self.llm_classifier:
                try:
                    llm_result = await self.llm_classifier.classify(title, text)
                    final = self._merge_results(rule_result, llm_result)
                    llm_used += 1
                except Exception as e:
                    logger.warning("LLM classification for '%s': %s", title[:50], e)
                    final = rule_result
            else:
                final = rule_result

            classified.append(ClassifiedReport(
                search_result=sr,
                classification=final,
            ))

            msg = f"Classified {i + 1}/{total}: {title[:40]}..."
            state["messages"].append(msg)
            if self.progress_cb:
                self.progress_cb("classifying", msg)

        state["classified_reports"] = classified
        state["classification_status"] = "done"
        method = f"hybrid (LLM: {llm_used}/{total})" if llm_used else "rule-only"
        state["messages"].append(
            f"Classification complete: {len(classified)} reports, method: {method}"
        )
        return state

    def _merge_results(
        self, rule: ClassificationResult, llm: ClassificationResult
    ) -> ClassificationResult:
        markets = list(set(rule.markets) | set(llm.markets))
        asset_classes = list(set(rule.asset_classes) | set(llm.asset_classes))
        frequencies = list(set(rule.frequencies) | set(llm.frequencies))
        topics = list(set(rule.topics) | set(llm.topics))
        custom_tags = list(set(rule.custom_tags) | set(llm.custom_tags))

        confidence = max(rule.confidence, llm.confidence)

        return ClassificationResult(
            markets=markets,
            asset_classes=asset_classes,
            frequencies=frequencies,
            topics=topics,
            custom_tags=custom_tags,
            confidence=confidence,
            method="hybrid",
        )
