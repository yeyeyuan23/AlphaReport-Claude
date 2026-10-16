from __future__ import annotations

import logging

from reportagent.classifiers.taxonomy import load_taxonomy
from reportagent.llm.client import LLMClient
from reportagent.models.schemas import (
    ClassificationResult,
    Market,
    AssetClass,
    Frequency,
    Topic,
)

logger = logging.getLogger(__name__)

_ENUM_MAP = {
    "markets": Market,
    "asset_classes": AssetClass,
    "frequencies": Frequency,
    "topics": Topic,
}

SYSTEM_PROMPT = (
    "你是量化金融研报多标签分类专家。\n"
    "任务：根据研报标题和摘要，从多个维度为研报打标签。\n"
    "重要规则：\n"
    "1. 每个维度可以且应该选择多个匹配的值（多标签），不要只选一个\n"
    "2. 只要研报内容涉及该标签就应该选上，宁多勿少\n"
    "3. custom_tags 是你基于摘要内容提取的具体方法/技术标签（中文），"
    "例如：\"LSTM选股\"、\"风险平价\"、\"动量因子\"、\"期权隐含波动率\"。"
    "每篇研报应至少提取 2-5 个 custom_tags\n"
    "4. 如果研报语言为中文，custom_tags 用中文；如果为英文，用英文\n"
)


class LLMClassifier:
    def __init__(self, llm_client: LLMClient):
        self.llm_client = llm_client

    async def classify(self, title: str, text: str) -> ClassificationResult:
        taxonomy = load_taxonomy()
        taxonomy_desc = self._format_taxonomy(taxonomy)

        prompt = (
            f"## 分类维度与可选值\n{taxonomy_desc}\n\n"
            f"## 研报标题\n{title}\n\n"
            f"## 研报摘要\n{text[:3000]}\n\n"
            "请返回 JSON，每个维度尽量选出所有匹配的值：\n"
            '- "markets": list，市场维度值 (如 ["china", "global"])\n'
            '- "asset_classes": list，资产类别值\n'
            '- "frequencies": list，交易频率值\n'
            '- "topics": list，研究主题值（多选！一篇研报通常涉及 2-4 个主题）\n'
            '- "custom_tags": list，从摘要中提取的具体方法/技术/模型标签，'
            '如 ["多因子选股", "XGBoost", "沪深300增强"]，至少 2 个\n'
            '- "confidence": float 0-1\n'
        )

        try:
            result = await self.llm_client.chat_json(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ]
            )
            return self._parse_result(result)
        except Exception as e:
            logger.warning("LLM classification failed: %s", e)
            return ClassificationResult(confidence=0.0, method="llm")

    def _format_taxonomy(self, taxonomy: dict) -> str:
        lines = []
        for dim_key, dim_data in taxonomy.items():
            label = dim_data.get("label", dim_key)
            lines.append(f"### {label}")
            for val_key, val_data in dim_data.get("values", {}).items():
                val_label = val_data.get("label", val_key)
                lines.append(f"  - {val_key}: {val_label}")
            lines.append("")
        return "\n".join(lines)

    def _parse_result(self, data: dict) -> ClassificationResult:
        markets = self._safe_enum_list(data.get("markets", []), Market)
        asset_classes = self._safe_enum_list(data.get("asset_classes", []), AssetClass)
        frequencies = self._safe_enum_list(data.get("frequencies", []), Frequency)
        topics = self._safe_enum_list(data.get("topics", []), Topic)
        custom_tags = data.get("custom_tags", [])
        confidence = float(data.get("confidence", 0.5))

        return ClassificationResult(
            markets=markets,
            asset_classes=asset_classes,
            frequencies=frequencies,
            topics=topics,
            custom_tags=custom_tags if isinstance(custom_tags, list) else [],
            confidence=min(max(confidence, 0.0), 1.0),
            method="llm",
        )

    def _safe_enum_list(self, values: list, enum_cls) -> list:
        result = []
        valid = {e.value for e in enum_cls}
        for v in values:
            if isinstance(v, str) and v in valid:
                result.append(enum_cls(v))
        return result
