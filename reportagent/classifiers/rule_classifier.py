from __future__ import annotations

from reportagent.classifiers.taxonomy import get_dimension_keywords, get_all_dimensions
from reportagent.models.schemas import (
    ClassificationResult,
    Market,
    AssetClass,
    Frequency,
    Topic,
)

_ENUM_MAP = {
    "market": Market,
    "asset_class": AssetClass,
    "frequency": Frequency,
    "topic": Topic,
}

_MATCH_THRESHOLD = 0.3


class RuleClassifier:
    def classify(self, title: str, text: str) -> ClassificationResult:
        search_text = (title + " " + text[:3000]).lower()

        all_scores: dict[str, dict[str, float]] = {}
        matched_values: dict[str, list] = {}

        for dim in get_all_dimensions():
            kw_map = get_dimension_keywords(dim)
            dim_scores: dict[str, float] = {}

            for value, keywords in kw_map.items():
                if not keywords:
                    continue
                match_count = sum(1 for kw in keywords if kw.lower() in search_text)
                score = match_count / len(keywords) if keywords else 0.0
                dim_scores[value] = score

            all_scores[dim] = dim_scores
            enum_cls = _ENUM_MAP.get(dim)
            if enum_cls:
                matched_values[dim] = [
                    enum_cls(v) for v, s in dim_scores.items()
                    if s >= _MATCH_THRESHOLD and v in [e.value for e in enum_cls]
                ]
            else:
                matched_values[dim] = [
                    v for v, s in dim_scores.items() if s >= _MATCH_THRESHOLD
                ]

        confidences = []
        for dim, scores in all_scores.items():
            if scores:
                best = max(scores.values())
                confidences.append(best)
        avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

        return ClassificationResult(
            markets=matched_values.get("market", []),
            asset_classes=matched_values.get("asset_class", []),
            frequencies=matched_values.get("frequency", []),
            topics=matched_values.get("topic", []),
            confidence=min(avg_confidence, 1.0),
            method="rule",
        )
