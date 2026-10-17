from __future__ import annotations

import logging
import math
import json
from time import monotonic

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


class ClassificationValidationError(ValueError):
    """A bounded description of a model response field, safe for diagnostics."""

    def __init__(self, field: str, expected: str, actual):
        value = json.dumps(actual, ensure_ascii=False, default=lambda _: "<object>")
        super().__init__(f"{field}: expected {expected}; received {value[:300]}")


def classification_error_detail(error: Exception) -> str:
    # Arbitrary SDK exceptions may include credentials or transport URLs.
    if isinstance(error, (ClassificationValidationError, ClassificationEvidenceError)):
        return f"{type(error).__name__}: {error}"
    return type(error).__name__


class LLMClassifier:
    def __init__(self, llm_client: LLMClient):
        self.llm_client = llm_client

    async def classify(self, title: str, text: str) -> ClassificationResult:
        started = monotonic()
        messages = self._messages(title, text)
        logger.info(
            "Classification start title=%r input_sha256=%s model=%s",
            title[:160],
            input_fingerprint(title, text),
            getattr(self.llm_client, "model", "unknown"),
        )
        try:
            result = await self.llm_client.chat_json(messages)
            result, parsed = await self._parse_with_schema_repair(
                messages, result, title, text
            )
            if parsed.provenance["rejected_labels"]:
                parsed = await self._repair_quotes(
                    messages, result, parsed, title, text
                )
            logger.info(
                "Classification completed title=%r input_sha256=%s elapsed_s=%.3f labels=%s",
                title[:160],
                input_fingerprint(title, text),
                monotonic() - started,
                sorted(parsed.provenance["labels"]),
            )
            return parsed
        except Exception as error:
            logger.warning(
                "Classification failed title=%r input_sha256=%s elapsed_s=%.3f: %s",
                title[:160],
                input_fingerprint(title, text),
                monotonic() - started,
                classification_error_detail(error),
            )
            raise

    def _messages(self, title: str, text: str) -> list[dict]:
        taxonomy = load_taxonomy()
        taxonomy_desc = self._format_taxonomy(taxonomy)

        allowed = {
            field: [entry.value for entry in enum] for field, enum in _ENUM_MAP.items()
        }
        prompt = (
            "以下是返回字段与允许值的完整映射，必须原样使用机器值，不得自造同义词（如 high_freq）：\n"
            + json.dumps(allowed, ensure_ascii=False)
            + "\n\n"
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

        return [
            {
                "role": "system",
                "content": skill_resource(
                    "daily-quant-scan", "classification.md"
                ).read_text(encoding="utf-8"),
            },
            {"role": "user", "content": prompt},
        ]

    async def _request_repair(self, messages, result, instruction):
        return await self.llm_client.chat_json(
            messages
            + [
                {
                    "role": "assistant",
                    "content": json.dumps(result, ensure_ascii=False),
                },
                {"role": "user", "content": instruction},
            ]
        )

    async def _parse_with_schema_repair(self, messages, result, title, text):
        try:
            return result, self._parse_result(result, title, text)
        except ClassificationValidationError as error:
            detail = classification_error_detail(error)
            logger.warning(
                "Classification schema repair title=%r input_sha256=%s: %s",
                title[:160],
                input_fingerprint(title, text),
                detail,
            )
        # Only response shape/taxonomy errors get one correction. Never turn an
        # SDK error into a format repair or relax the original evidence checks.
        corrected = await self._request_repair(
            messages,
            result,
            "分类 JSON 未通过字段校验："
            + detail
            + "。请对照原始字段与允许值返回完整 JSON。枚举必须原样使用，不能自造同义词；"
            "只保留原文直接支持的标签及连续引文。没有合适类别就留空。",
        )
        parsed = self._parse_result(corrected, title, text)
        parsed.provenance.update(
            schema_repair_status="completed", initial_schema_error=detail
        )
        return corrected, parsed

    async def _repair_quotes(self, messages, result, parsed, title, text):
        rejected = parsed.provenance["rejected_labels"]
        parsed.provenance.update(
            repair_attempted=True, initial_rejected_labels=rejected
        )
        try:
            repaired = await self._request_repair(
                messages,
                result,
                "以下标签的引文无法在指定来源中准确定位，或不含实质内容："
                + json.dumps(rejected, ensure_ascii=False)
                + "。请返回完整分类 JSON。仅修正证据为真实的连续原文（保留断行、连字符和大小写），"
                "或删除确无支持的标签；不得新增标签。优先选择足够支持判断的简短引文。",
            )
            corrected = self._parse_result(repaired, title, text)
            original = {
                f"{field}.{value}"
                for field in (*_ENUM_MAP, "custom_tags")
                for value in result[field]
            }
            if not set(corrected.provenance["labels"]).issubset(original):
                raise ClassificationValidationError(
                    "quote_repair.labels",
                    "a subset of original labels",
                    sorted(corrected.provenance["labels"]),
                )
            corrected.provenance.update(
                **{
                    key: value
                    for key, value in parsed.provenance.items()
                    if key in {"schema_repair_status", "initial_schema_error"}
                },
                repair_attempted=True,
                initial_rejected_labels=rejected,
                repair_status="partial"
                if corrected.provenance["rejected_labels"]
                else "completed",
            )
            return corrected
        except Exception as error:
            logger.warning(
                "Classification quote repair failed title=%r input_sha256=%s: %s",
                title[:160],
                input_fingerprint(title, text),
                classification_error_detail(error),
            )
            parsed.provenance["repair_status"] = "failed"
            return parsed

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
            raise ClassificationValidationError("response", "an object", data)
        for field, enum_cls in _ENUM_MAP.items():
            values = data.get(field)
            valid = {entry.value for entry in enum_cls}
            if not isinstance(values, list) or any(
                not isinstance(v, str) or v not in valid for v in values
            ):
                raise ClassificationValidationError(
                    field, f"a list drawn from {sorted(valid)}", values
                )
        custom_tags = data.get("custom_tags")
        if not isinstance(custom_tags, list) or any(
            not isinstance(v, str) or not v.strip() for v in custom_tags
        ):
            raise ClassificationValidationError(
                "custom_tags", "a list of nonempty strings", custom_tags
            )
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
            raise ClassificationValidationError(
                "confidence", "a finite number between 0 and 1", confidence
            )
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
