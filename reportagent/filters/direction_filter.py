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
from pathlib import Path
from typing import Optional

import yaml

from reportagent.utils.config import PROJECT_ROOT
from reportagent.skills.registry import skill_resource

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
    max_score: float = 0.0
    best_direction: str = ""

    @property
    def is_selected(self) -> bool:
        """是否被选中推送：至少有一个方向 >= 阈值"""
        return self.max_score >= 6.0


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

    def _load_directions(self):
        """从 configs/push_directions.yaml 加载研究方向定义。"""
        path = PROJECT_ROOT / "configs" / "push_directions.yaml"
        if not path.exists():
            logger.warning("push_directions.yaml not found at %s", path)
            return
        with open(path) as f:
            data = yaml.safe_load(f)
        self._directions = [
            d for d in data.get("directions", [])
            if d.get("enabled", True)
        ]
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
            abstract_short = (p.get("abstract", "") or "")[:500]
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

        # Lazy init LLM client
        if self._llm_client is None:
            from reportagent.llm.client import LLMClient
            self._llm_client = LLMClient()

        results: list[PaperDirectionResult] = []

        # Batch process
        for batch_start in range(0, len(papers), self.BATCH_SIZE):
            batch = papers[batch_start:batch_start + self.BATCH_SIZE]
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
                results.extend(self._empty_results(batch))

            # Rate limit between batches
            if batch_start + self.BATCH_SIZE < len(papers):
                await asyncio.sleep(0.5)

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
        items = resp if isinstance(resp, list) else resp.get("papers", resp.get("results", []))

        # Build direction lookup
        dir_map = {d["id"]: d["name"] for d in self._directions}

        results = []
        paper_by_id = {p["id"]: p for p in papers}

        for item in items:
            rid = item.get("report_id", 0)
            paper = paper_by_id.get(rid)
            if not paper:
                continue

            scores = []
            max_score = 0.0
            best_dir = ""

            for s in item.get("scores", []):
                did = s.get("direction_id", "")
                score = float(s.get("score", 0))
                reason = s.get("reason", "")

                if did not in dir_map:
                    continue

                ds = DirectionScore(
                    direction_id=did,
                    direction_name=dir_map[did],
                    score=score,
                    reason=reason,
                )
                scores.append(ds)

                if score > max_score:
                    max_score = score
                    best_dir = dir_map[did]

            results.append(PaperDirectionResult(
                report_id=rid,
                title=paper.get("title", ""),
                abstract=paper.get("abstract", "") or "",
                topics=paper.get("topics", ""),
                direction_scores=scores,
                max_score=max_score,
                best_direction=best_dir,
            ))

        return results

    def _empty_results(self, papers: list[dict]) -> list[PaperDirectionResult]:
        """Return empty results when LLM is unavailable."""
        return [
            PaperDirectionResult(
                report_id=p["id"],
                title=p.get("title", ""),
                abstract=p.get("abstract", "") or "",
                topics=p.get("topics", ""),
            )
            for p in papers
        ]

    def get_selected_papers(
        self, results: list[PaperDirectionResult], threshold: float | None = None
    ) -> list[PaperDirectionResult]:
        """返回被选中的论文（至少一个方向 >= 阈值）。"""
        t = threshold if threshold is not None else self.SELECTION_THRESHOLD
        return [r for r in results if r.max_score >= t]

    def format_scores_for_briefing(
        self, results: list[PaperDirectionResult]
    ) -> str:
        """格式化方向评分信息，供 Claude 简报使用。"""
        lines = ["## 🎯 LLM 方向评分结果\n"]
        for r in sorted(results, key=lambda x: x.max_score, reverse=True):
            if not r.is_selected:
                continue
            top3 = sorted(r.direction_scores, key=lambda x: x.score, reverse=True)[:3]
            score_str = " | ".join(
                f"{s.direction_name}: {s.score:.1f}" for s in top3
            )
            lines.append(f"- **{r.title[:60]}**")
            lines.append(f"  最高分: {score_str}")
        return "\n".join(lines)
