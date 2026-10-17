from __future__ import annotations

import logging
import math
import json

from reportagent.classifiers.taxonomy import load_taxonomy
from reportagent.classifiers.provenance import input_fingerprint
from reportagent.llm.client import LLMClient
from reportagent.skills.registry import skill_resource
from reportagent.services.text_evidence import locate_quote
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


class ClassificationEvidenceError(ValueError):
    """The model supplied no attributable labels; rule guesses are not a repair."""


class LLMClassifier:
    def __init__(self, llm_client: LLMClient):
        self.llm_client = llm_client

    async def classify(self, title: str, text: str) -> ClassificationResult:
        taxonomy = load_taxonomy()
        taxonomy_desc = self._format_taxonomy(taxonomy)

        prompt = (
            f"## 分类维度与可选值\n{taxonomy_desc}\n\n"
            f"## 研报标题\n{title}\n\n"
            f"## 研报分类材料（原文摘要，缺失时使用注明来源的引言）\n{text}\n\n"
            "请返回 JSON，只选有直接证据的标签，每个维度都允许 []，不设标签数量目标：\n"
            '- "markets": list，市场维度值 (如 ["china", "global"])\n'
            '- "asset_classes": list，资产类别值\n'
            '- "frequencies": list，交易频率值\n'
            '- "topics": list，论文实际研究的主题值；不把背景提及的主题算作本文研究\n'
            '- "custom_tags": list，从原文材料中提取的具体方法/技术/模型标签，'
            '如 ["多因子选股", "XGBoost", "沪深300增强"]；没有依据时返回 []\n'
            '- "confidence": float 0-1\n'
            '- "evidence": object，每个已选标签必须有一项，键为 "维度.标签值"，'
            '例如 "topics.ai_ml_model" 或 "custom_tags.Transformer"。值为 '
            '{"source":"title"或"material", "quote":"该来源中连续的原文引文", '
            '"reason":"说明引文如何满足该标签定义；不能只重复标签名"}。'
            "缺少直接证据的标签不要返回。引文不得改写、翻译或拼接。\n"
        )

        try:
            messages = [
                {
                    "role": "system",
                    "content": skill_resource(
                        "daily-quant-scan", "classification.md"
                    ).read_text(encoding="utf-8"),
                },
                {"role": "user", "content": prompt},
            ]
            result = await self.llm_client.chat_json(messages)
            parsed = self._parse_result(result, title, text)
            rejected = parsed.provenance["rejected_labels"]
            if rejected:
                # One bounded repair, with the original source still in context.
                # Never loosen matching or accept a hallucinated quote on retry.
                parsed.provenance.update(
                    repair_attempted=True, initial_rejected_labels=rejected
                )
                try:
                    repaired = await self.llm_client.chat_json(
                        messages
                        + [
                            {
                                "role": "assistant",
                                "content": json.dumps(result, ensure_ascii=False),
                            },
                            {
                                "role": "user",
                                "content": "以下标签的引文无法在指定来源中准确定位，或不含实质内容："
                                + json.dumps(rejected, ensure_ascii=False)
                                + "。请返回完整分类 JSON。仅修正证据为真实的连续原文（保留断行、连字符和大小写），"
                                "或删除确无支持的标签；不得新增标签。优先选择足够支持判断的简短引文。",
                            },
                        ]
                    )
                    corrected = self._parse_result(repaired, title, text)
                    # A quote repair cannot introduce unrelated new labels.
                    original = {
                        f"{field}.{value}"
                        for field in (*_ENUM_MAP, "custom_tags")
                        for value in result[field]
                    }
                    if not set(corrected.provenance["labels"]).issubset(original):
                        raise ValueError("Quote repair introduced new labels")
                    corrected.provenance.update(
                        repair_attempted=True,
                        initial_rejected_labels=rejected,
                        repair_status="partial"
                        if corrected.provenance["rejected_labels"]
                        else "completed",
                    )
                    parsed = corrected
                except Exception as error:
                    logger.warning(
                        "Classification quote repair failed: %s", type(error).__name__
                    )
                    parsed.provenance["repair_status"] = "failed"
            return parsed
        except Exception as e:
            logger.warning("LLM classification failed: %s", type(e).__name__)
            raise

    def _format_taxonomy(self, taxonomy: dict) -> str:
        lines = []
        for dim_key, dim_data in taxonomy.items():
            label = dim_data.get("label", dim_key)
            lines.append(f"### {label}")
            for val_key, val_data in dim_data.get("values", {}).items():
                val_label = val_data.get("label", val_key)
                description = val_data.get("description", "")
                lines.append(f"  - {val_key}: {val_label}。{description}")
            lines.append("")
        return "\n".join(lines)

    def _parse_result(self, data: dict, title: str, text: str) -> ClassificationResult:
        if not isinstance(data, dict):
            raise ValueError("Classification must be an object")
        for field, enum_cls in _ENUM_MAP.items():
            values = data.get(field)
            valid = {entry.value for entry in enum_cls}
            if not isinstance(values, list) or any(
                not isinstance(v, str) or v not in valid for v in values
            ):
                raise ValueError(f"{field} must contain only valid taxonomy values")
        custom_tags = data.get("custom_tags")
        if not isinstance(custom_tags, list) or any(
            not isinstance(v, str) or not v.strip() for v in custom_tags
        ):
            raise ValueError("custom_tags must be a list of nonempty strings")
        if not custom_tags and not any(data[field] for field in _ENUM_MAP):
            raise ClassificationEvidenceError(
                "Model returned no usable classification evidence; existing tags must be retained"
            )
        confidence = data.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise ValueError("confidence must be a finite number between 0 and 1")
        # Verify that every label has an actual quote in the material supplied to
        # this call. This checks attribution, not semantic entailment: category
        # boundaries and the reason are still assessed by the model and review.
        evidence = data.get("evidence")
        if not isinstance(evidence, dict):
            raise ClassificationEvidenceError(
                "Classification evidence must be an object"
            )
        supported, rejected = {}, []
        selected = {}
        for field in (*_ENUM_MAP, "custom_tags"):
            selected[field] = []
            for value in dict.fromkeys(data[field]):
                key = f"{field}.{value}"
                item = evidence.get(key)
                source = item.get("source") if isinstance(item, dict) else None
                quote = item.get("quote") if isinstance(item, dict) else None
                reason = item.get("reason") if isinstance(item, dict) else None
                material = (
                    {"title": title, "material": text}.get(source)
                    if isinstance(source, str)
                    else None
                )
                match = (
                    locate_quote(material, quote)
                    if (
                        material
                        and isinstance(quote, str)
                        and sum(char.isalnum() for char in quote) >= 2
                        and isinstance(reason, str)
                        and reason.strip()
                    )
                    else None
                )
                if match is None:
                    rejected.append(key)
                    continue
                selected[field].append(value)
                supported[key] = {
                    "source": source,
                    "quote": match["raw_quote"],
                    "reason": reason.strip(),
                    "raw_start": match["raw_start"],
                    "raw_end": match["raw_end"],
                }
        if not supported:
            raise ClassificationEvidenceError(
                "No labels supported by verifiable classification evidence"
            )
        data = {**data, **selected}
        custom_tags = data["custom_tags"]
        markets = self._safe_enum_list(data["markets"], Market)
        asset_classes = self._safe_enum_list(data["asset_classes"], AssetClass)
        frequencies = self._safe_enum_list(data["frequencies"], Frequency)
        topics = self._safe_enum_list(data["topics"], Topic)

        return ClassificationResult(
            markets=markets,
            asset_classes=asset_classes,
            frequencies=frequencies,
            topics=topics,
            custom_tags=custom_tags if isinstance(custom_tags, list) else [],
            confidence=min(max(confidence, 0.0), 1.0),
            method="llm",
            provenance={
                "schema_version": 1,
                "input_sha256": input_fingerprint(title, text),
                "labels": supported,
                "rejected_labels": rejected,
            },
        )

    def _safe_enum_list(self, values: list, enum_cls) -> list:
        result = []
        valid = {e.value for e in enum_cls}
        for v in values:
            if isinstance(v, str) and v in valid:
                result.append(enum_cls(v))
        return result
