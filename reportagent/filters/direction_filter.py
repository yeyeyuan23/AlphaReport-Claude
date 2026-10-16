"""
LLM Direction Filter — 基于6大研究方向对论文评分过滤

全量拉取后，用 LLM 对每篇论文在6个推送方向上打分，
只保留高分论文进入精选推送。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field


from reportagent.skills.registry import skill_resource
from reportagent.services.paper_scoring import (
    _valid_score, _configured_threshold, _load_scoring_config,
    load_enabled_directions, has_screening_material,
)

logger = logging.getLogger(__name__)

# ── 6大研究方向定义（从 push_directions.yaml 加载）──

@dataclass
class DirectionScore:
    """单篇论文在某一方向上的评分"""
    direction_id: str
    direction_name: str
    score: float          # 0-10 分
    reason: str           # 一句话理由


@dataclass
class PaperDirectionResult:
    """单篇论文的多方向评分结果"""
    report_id: int
    title: str
    abstract: str
    topics: str
    direction_scores: list[DirectionScore] = field(default_factory=list)
    max_score: float | None = None
    best_direction: str = ""
    status: str = "retryable_error"
    reason: str = "尚未完成方向评分"
    threshold: float = 6.0

    @property
    def is_selected(self) -> bool:
        """是否被选中推送：至少有一个方向 >= 阈值"""
        return self.status == "scored" and self.max_score is not None and self.max_score >= self.threshold


class DirectionFilter:
    """LLM 驱动的多方向论文过滤器。

    对每篇论文，用 LLM 评估其在 6 个研究方向上的相关性（0-10分），
    保留总分足够高的论文进入精选推送。
    """

    # 批量评分：每次送 N 篇论文给 LLM
    BATCH_SIZE = 5

    # 精选阈值：单方向 >= 此分才算命中
    SELECTION_THRESHOLD = 6.0

    def __init__(self, llm_client=None):
        self._llm_client = llm_client
        self._directions: list[dict] = []
        self._load_directions()
        self.SELECTION_THRESHOLD = _configured_threshold(_load_scoring_config())

    def _load_directions(self):
        """从 configs/push_directions.yaml 加载研究方向定义。"""
        self._directions = list(load_enabled_directions().values())
        logger.info("Loaded %d push directions for LLM filtering", len(self._directions))

    @property
    def direction_names(self) -> list[str]:
        return [d["name"] for d in self._directions]

    def build_scoring_prompt(self, papers: list[dict]) -> str:
        """构建 LLM 评分 prompt。"""
        directions_desc = []
        for i, d in enumerate(self._directions, 1):
            kw_list = d.get("keywords", [])
            directions_desc.append(
                f"{i}. **{d['name']}** ({d['id']})\n"
                f"   描述：{d.get('description', '')}\n"
                f"   关键词：{', '.join(kw_list[:15])}"
                + ("..." if len(kw_list) > 15 else "")
            )

        papers_desc = []
        for i, p in enumerate(papers, 1):
            abstract_short = p.get("abstract", "") or ""
            papers_desc.append(
                f"### 论文 {i}\n"
                f"ID: {p['id']}\n"
                f"标题: {p['title']}\n"
                f"摘要: {abstract_short}\n"
                f"已有分类: {p.get('topics', '')}\n"
            )

        prompt = skill_resource("paper-selection", "direction-scoring.md").read_text(encoding="utf-8").format(
            directions_desc="\n".join(directions_desc), papers_desc="\n".join(papers_desc),
        )
        return prompt

    async def score_papers(self, papers: list[dict]) -> list[PaperDirectionResult]:
        """对一批论文进行方向评分。"""
        if not self._directions:
            logger.warning("No push directions loaded, skipping LLM filter")
            return self._empty_results(papers)

        if not papers:
            return []

        _valid_score(self.SELECTION_THRESHOLD, "threshold")
        eligible = [p for p in papers if has_screening_material(p)]
        if not eligible:
            return self._empty_results(papers, "pending_needs_material", "缺少可用摘要，待补充材料")

        # Lazy init LLM client
        if self._llm_client is None:
            from reportagent.llm.client import LLMClient
            try:
                self._llm_client = LLMClient()
            except Exception:
                return [self._empty_results([p], reason="模型初始化失败，待重试")[0]
                        if has_screening_material(p) else self._empty_results(
                            [p], "pending_needs_material", "缺少可用摘要，待补充材料")[0]
                        for p in papers]

        results: list[PaperDirectionResult] = []

        # Batch process
        for batch_start in range(0, len(eligible), self.BATCH_SIZE):
            batch = eligible[batch_start:batch_start + self.BATCH_SIZE]
            logger.info(
                "Scoring batch %d-%d/%d papers...",
                batch_start + 1, batch_start + len(batch), len(papers)
            )

            try:
                prompt = self.build_scoring_prompt(batch)
                resp = await self._llm_client.chat_json(
                    [{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=4096,
                )

                batch_results = self._parse_response(resp, batch)
                results.extend(batch_results)

            except Exception as e:
                logger.error("LLM direction scoring failed for batch: %s", e)
                # Fallback: empty scores for this batch
                results.extend(self._empty_results(batch, reason="模型评分失败，待重试"))

            # Rate limit between batches
            if batch_start + self.BATCH_SIZE < len(eligible):
                await asyncio.sleep(0.5)

        by_id = {r.report_id: r for r in results}
        results = [by_id[p["id"]] if p["id"] in by_id else
                   self._empty_results([p], "pending_needs_material", "缺少可用摘要，待补充材料")[0]
                   for p in papers]

        # Log summary
        selected = [r for r in results if r.is_selected]
        logger.info(
            "Direction scoring complete: %d/%d papers selected (threshold %.1f)",
            len(selected), len(results), self.SELECTION_THRESHOLD
        )
        for r in selected:
            logger.info("  ✓ [%s] %.1f — %s", r.best_direction, r.max_score, r.title[:50])

        return results

    def _parse_response(
        self, resp: dict | list, papers: list[dict]
    ) -> list[PaperDirectionResult]:
        """Parse LLM response into PaperDirectionResult list."""
        items = resp if isinstance(resp, list) else (resp.get("papers", resp.get("results", [])) if isinstance(resp, dict) else [])
        if not isinstance(items, list):
            items = []
        indexed = {}
        for item in items:
            if isinstance(item, dict) and type(item.get("report_id")) is int:
                indexed.setdefault(item["report_id"], []).append(item)
        dir_map = {d["id"]: d["name"] for d in self._directions}
        results = []
        for paper in papers:
            result = self._empty_results([paper])[0]
            matches = indexed.get(paper["id"], [])
            try:
                if len(matches) != 1:
                    raise ValueError("论文评分缺失或重复")
                item = matches[0]
                if item.get("status", "scored") not in ("scored", "pending_needs_material"):
                    raise ValueError("未知或失败的评分状态")
                if item.get("status") == "pending_needs_material":
                    if item.get("scores") not in (None, []):
                        raise ValueError("材料不足时不能附带分数")
                    result.status = "pending_needs_material"
                    result.reason = str(item.get("reason") or "需要补充研究摘要")
                    results.append(result)
                    continue
                scores = item.get("scores")
                if not isinstance(scores, list) or not scores:
                    raise ValueError("缺少方向评分")
                checked = {}
                for score_item in scores:
                    if not isinstance(score_item, dict):
                        raise ValueError("方向评分格式错误")
                    did = score_item.get("direction_id")
                    if not isinstance(did, str) or did not in dir_map or did in checked:
                        raise ValueError("方向未知、停用或重复")
                    score = _valid_score(score_item.get("score"), "score")
                    reason = score_item.get("reason")
                    if not isinstance(reason, str) or not reason.strip():
                        raise ValueError("缺少评分依据")
                    checked[did] = DirectionScore(did, dir_map[did], score, reason.strip())
                if set(checked) != set(dir_map):
                    raise ValueError("未覆盖全部启用方向")
                result.direction_scores = list(checked.values())
                best = max(result.direction_scores, key=lambda x: x.score)
                result.max_score = best.score
                result.best_direction = best.direction_id if best.score > 0 else ""
                result.status, result.reason = "scored", best.reason
            except (ValueError, TypeError):
                result.reason = "模型结果缺失或无效，待重试"
            results.append(result)
        return results

    def _empty_results(self, papers: list[dict], status="retryable_error", reason="尚未完成方向评分") -> list[PaperDirectionResult]:
        return [PaperDirectionResult(
            report_id=p["id"], title=p.get("title", ""), abstract=p.get("abstract", "") or "",
            topics=p.get("topics", ""), status=status, reason=reason,
            threshold=_valid_score(self.SELECTION_THRESHOLD, "threshold"),
        ) for p in papers]

    def get_selected_papers(
        self, results: list[PaperDirectionResult], threshold: float | None = None
    ) -> list[PaperDirectionResult]:
        """返回被选中的论文（至少一个方向 >= 阈值）。"""
        t = threshold if threshold is not None else self.SELECTION_THRESHOLD
        _valid_score(t, "threshold")
        return [r for r in results if r.status == "scored" and r.max_score is not None and r.max_score >= t]

    def format_scores_for_briefing(
        self, results: list[PaperDirectionResult]
    ) -> str:
        """格式化方向评分信息，供 Claude 简报使用。"""
        lines = ["## 🎯 LLM 方向评分结果\n"]
        for r in sorted(results, key=lambda x: x.max_score if x.max_score is not None else -1, reverse=True):
            if not r.is_selected:
                continue
            top3 = sorted(r.direction_scores, key=lambda x: x.score, reverse=True)[:3]
            score_str = " | ".join(
                f"{s.direction_name}: {s.score:.1f}" for s in top3
            )
            lines.append(f"- **{r.title[:60]}**")
            lines.append(f"  最高分: {score_str}")
        return "\n".join(lines)
