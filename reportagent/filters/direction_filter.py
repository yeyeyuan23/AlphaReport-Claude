"""
LLM Direction Filter — 基于6大研究方向对论文评分过滤

全量拉取后，用 LLM 对每篇论文在6个推送方向上打分，
只保留高分论文进入精选推送。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field


from reportagent.skills.registry import skill_resource
from reportagent.services.paper_scoring import (
    _valid_score,
    _configured_threshold,
    _load_scoring_config,
    load_enabled_directions,
    has_screening_material,
)

logger = logging.getLogger(__name__)

# ── 6大研究方向定义（从 push_directions.yaml 加载）──


@dataclass
class DirectionScore:
    """单篇论文在某一方向上的评分"""

    direction_id: str
    direction_name: str
    score: float  # 0-10 分
    reason: str  # 一句话理由


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
    assessment: dict | None = field(default=None, repr=False)

    @property
    def is_selected(self) -> bool:
        """是否被选中推送：至少有一个方向 >= 阈值"""
        return (
            self.status == "scored"
            and self.max_score is not None
            and self.max_score >= self.threshold
        )


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
        self.failed_batches: list[tuple] = []
        self._directions: list[dict] = []
        self._load_directions()
        self.SELECTION_THRESHOLD = _configured_threshold(_load_scoring_config())

    def _load_directions(self):
        """从 configs/push_directions.yaml 加载研究方向定义。"""
        self._directions = list(load_enabled_directions().values())
        logger.info(
            "Loaded %d push directions for LLM filtering", len(self._directions)
        )

    @property
    def direction_names(self) -> list[str]:
        return [d["name"] for d in self._directions]

    def build_scoring_prompt(self, papers: list[dict]) -> str:
        """Compatibility preview using the canonical prompt and schema."""
        from reportagent.services import paper_scoring as scoring

        directions = {value["id"]: value for value in self._directions}
        system = skill_resource("paper-selection", "scoring-system.md").read_text(
            encoding="utf-8"
        )
        user = skill_resource("paper-selection", "scoring-user.md").read_text(
            encoding="utf-8"
        )
        return (
            system.format(directions_desc=scoring._build_directions_desc(directions))
            + "\n"
            + user.format(
                papers_text=scoring._format_papers_for_llm(papers),
                direction_ids=", ".join(directions),
            )
        )

    async def score_papers(self, papers: list[dict]) -> list[PaperDirectionResult]:
        """Detailed view of the same relevance execution used by collection and reports."""
        from reportagent.services import paper_scoring as scoring

        self.failed_batches = []
        if not papers:
            return []
        _valid_score(self.SELECTION_THRESHOLD, "threshold")
        needs_model = any(has_screening_material(paper) for paper in papers)
        owned = needs_model and self._llm_client is None
        try:
            if owned:
                from reportagent.llm.client import LLMClient

                self._llm_client = LLMClient()
            assessed, self.failed_batches = await scoring.score_papers_batch(
                self._llm_client, papers, batch_size=self.BATCH_SIZE
            )
        except Exception as error:
            self.failed_batches = [(0, len(papers), str(error)[:200])]
            return self._empty_results(papers, reason=f"模型评分失败，待重试：{error}")
        finally:
            if owned and self._llm_client is not None:
                await self._llm_client._client.close()
                self._llm_client = None
        by_identity = {id(value["_paper"]): value for value in assessed}
        results = []
        for paper in papers:
            value = by_identity.get(id(paper))
            result = self._empty_results([paper])[0]
            if value is not None:
                if value["status"] == "pending_needs_material":
                    result.status, result.reason = value["status"], value["reason"]
                    result.assessment = value
                elif value["status"] == "scored":
                    result.direction_scores = [
                        DirectionScore(**detail)
                        for detail in value.get("direction_scores", [])
                    ]
                    result.max_score = value["relevance"]
                    result.best_direction = value["best_direction"] or ""
                    result.status, result.reason, result.assessment = (
                        "scored",
                        value["reason"],
                        value,
                    )
            results.append(result)
        return results

    def _empty_results(
        self, papers: list[dict], status="retryable_error", reason="尚未完成方向评分"
    ) -> list[PaperDirectionResult]:
        return [
            PaperDirectionResult(
                report_id=p["id"],
                title=p.get("title", ""),
                abstract=p.get("abstract", "") or "",
                topics=p.get("topics", ""),
                status=status,
                reason=reason,
                threshold=_valid_score(self.SELECTION_THRESHOLD, "threshold"),
            )
            for p in papers
        ]

    def get_selected_papers(
        self, results: list[PaperDirectionResult], threshold: float | None = None
    ) -> list[PaperDirectionResult]:
        """返回被选中的论文（至少一个方向 >= 阈值）。"""
        t = threshold if threshold is not None else self.SELECTION_THRESHOLD
        _valid_score(t, "threshold")
        return [
            r
            for r in results
            if r.status == "scored" and r.max_score is not None and r.max_score >= t
        ]
