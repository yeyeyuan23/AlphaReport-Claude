"""Screen abstracts for relevance, retaining invalid or stale assessments for retry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import hashlib
import json
import logging
import math
import re

import yaml
from dotenv import load_dotenv


from reportagent.db.engine import init_db, get_session_factory
from reportagent.db.repository import ReportRepository
from reportagent.llm.client import LLMClient
from reportagent.skills.registry import skill_resource
from reportagent.utils.config import PROJECT_ROOT

load_dotenv()

logger = logging.getLogger("llm_filter")


def relevance_reason_text(reason: str | None) -> str:
    """Read old decorated reasons without treating screening metadata as live status."""
    return re.sub(
        r"^(?:\[[^\]\n]+\]\s*)?\[(?:仅摘要相关性；全文质量待评审|材料不足待补全)\]\s*",
        "",
        (reason or "").strip(),
    ).strip()


@dataclass(frozen=True)
class ScoringOptions:
    days: int = 1
    all_unjudged: bool = False
    published_days: int | None = None
    threshold: float | None = None
    batch_size: int = 10
    max_papers: int = 200
    dry_run: bool = False


def _load_scoring_config() -> dict:
    path = PROJECT_ROOT / "configs" / "push_directions.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not isinstance(config.get("directions"), list):
        raise ValueError("push_directions.yaml must define a directions list")
    return config


def load_enabled_directions() -> dict[str, dict]:
    """Read current YAML; return enabled direction definitions keyed by their IDs."""
    return _enabled_directions(_load_scoring_config())


def _enabled_directions(config: dict) -> dict[str, dict]:
    directions = {}
    for item in config["directions"]:
        if not isinstance(item, dict):
            raise ValueError("Each research direction must be an object")
        if not item.get("enabled", True):
            continue
        direction_id = item.get("id")
        if not isinstance(direction_id, str) or not direction_id.strip():
            raise ValueError("Enabled research directions need non-empty IDs")
        if direction_id in directions:
            raise ValueError(f"Duplicate research direction: {direction_id}")
        directions[direction_id] = item
    return directions


def _valid_score(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number from 0 to 10")
    if not math.isfinite(value) or not 0 <= value <= 10:
        raise ValueError(f"{name} must be finite and between 0 and 10")
    return float(value)


def _configured_threshold(config: dict) -> float:
    return _valid_score(
        config.get("push_settings", {}).get("llm_filter_threshold", 6.0),
        "llm_filter_threshold",
    )


def _scoring_context() -> tuple[dict[str, dict], str, str, str]:
    config = _load_scoring_config()
    directions = _enabled_directions(config)
    system = skill_resource("paper-selection", "scoring-system.md").read_text(
        encoding="utf-8"
    )
    user = skill_resource("paper-selection", "scoring-user.md").read_text(
        encoding="utf-8"
    )
    payload = {
        "schema_version": 2,
        "directions": directions,
        "system_prompt": system,
        "user_prompt": user,
        "threshold": _configured_threshold(config),
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return directions, system, user, fingerprint


def scoring_fingerprint() -> str:
    """Identify the current relevance rubric, enabled directions and configured cutoff."""
    return _scoring_context()[3]


def _build_directions_desc(directions: dict[str, dict] | None = None) -> str:
    if directions is None:
        directions = load_enabled_directions()
    lines = []
    for direction_id, info in directions.items():
        keywords = ", ".join(info.get("keywords") or [])
        lines.append(
            f"**{info.get('name', direction_id)}** ({direction_id})\n"
            f"  关注：{info.get('description', '')}\n  关键词：{keywords}\n"
            f"  市场：{info.get('market', '')}；资产：{info.get('assets', [])}；"
            f"主题：{info.get('topics', [])}"
        )
    return "\n".join(lines)


def _paper_value(paper, field: str, default=""):
    return (
        paper.get(field, default)
        if isinstance(paper, dict)
        else getattr(paper, field, default)
    )


def evidence_fingerprint(paper) -> str:
    """Identify the title/abstract evidence actually used for relevance screening.

    Full text belongs to the separate review and analysis cache fingerprints;
    fetching it does not invalidate an otherwise unchanged abstract assessment.
    """
    payload = [_paper_value(paper, field) or "" for field in ("title", "abstract")]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def screening_is_current(paper) -> bool:
    """Only reuse an assessment produced from the current rubric and material."""
    try:
        provenance = json.loads(
            _paper_value(paper, "selection_provenance_json") or "{}"
        )
    except (TypeError, ValueError):
        return False
    return (
        isinstance(provenance, dict)
        and provenance.get("fingerprint") == scoring_fingerprint()
        and provenance.get("status") in ("scored", "pending_needs_material")
        and provenance.get("evidence_fingerprint") == evidence_fingerprint(paper)
    )


def has_screening_material(paper) -> bool:
    """Reject empty placeholders and bibliographic fields, not research conclusions.

    This guard only detects clear absence of research evidence. The model may still
    identify an otherwise plausible abstract as insufficient.
    """
    abstract = str(_paper_value(paper, "abstract") or "").strip()
    if abstract == str(_paper_value(paper, "title") or "").strip():
        return False
    abstract = re.sub(r"https?://\S+", "", abstract)
    abstract = re.sub(
        r"(?:机构|类型|页数|报告类型|研报类型|研究机构|来源|作者|发布时间|发布日期|institution|type|pages?)\s*[:：]\s*[^;；\n|]+",
        "",
        abstract,
        flags=re.I,
    )
    meaningful = re.sub(r"[\W_\d]+", "", abstract)
    return len(meaningful) >= 20


def _format_papers_for_llm(papers: list, indices=None) -> str:
    """Send complete abstracts with their original batch indexes."""
    indices = range(len(papers)) if indices is None else indices
    return "\n".join(
        f"### 论文 #{index}\n"
        f"标题：{_paper_value(paper, 'title')}\n"
        f"来源：{_paper_value(paper, 'source')}\n"
        f"分类标签：{_paper_value(paper, 'topics')}\n"
        f"摘要：{_paper_value(paper, 'abstract') or ''}\n"
        for index, paper in zip(indices, papers)
    )


def normalize_direction_scores(scores, directions: dict[str, dict]) -> list[dict]:
    """Validate one complete direction matrix for every scoring entrypoint."""
    if not isinstance(scores, list) or not scores:
        raise ValueError("Missing direction scores")
    checked = {}
    for value in scores:
        if not isinstance(value, dict):
            raise ValueError("Invalid direction score")
        direction = value.get("direction_id")
        if (
            not isinstance(direction, str)
            or direction not in directions
            or direction in checked
        ):
            raise ValueError("Unknown, disabled or duplicate direction")
        reason = value.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Missing direction evidence")
        checked[direction] = {
            "direction_id": direction,
            "direction_name": directions[direction].get("name", direction),
            "score": _valid_score(value.get("score"), "score"),
            "reason": reason.strip(),
        }
    if set(checked) != set(directions):
        raise ValueError("Direction scores must cover every enabled direction")
    return [checked[direction] for direction in directions]


def _normalize_assessment(item: dict, directions: dict[str, dict]) -> dict:
    if not isinstance(item, dict):
        raise ValueError("Each assessment must be an object")
    status = item.get("status", "scored")
    if status == "scored" and ("direction_scores" in item or "scores" in item):
        scores = normalize_direction_scores(
            item.get("direction_scores", item.get("scores")), directions
        )
        best = max(scores, key=lambda value: value["score"])
        item = {
            **item,
            "direction_scores": scores,
            "score": best["score"],
            "relevance": best["score"],
            "best_direction": best["direction_id"] if best["score"] > 0 else None,
            "reason": best["reason"],
        }
    if status not in ("scored", "pending_needs_material"):
        raise ValueError("Unknown relevance status")
    reason = item.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must explain abstract-only relevance")
    if status == "pending_needs_material":
        if (
            item.get("score") is not None
            or item.get("relevance") is not None
            or item.get("direction_scores") not in (None, [])
            or item.get("scores") not in (None, [])
        ):
            raise ValueError("Insufficient material requires null score and relevance")
        return {
            **item,
            "status": status,
            "score": None,
            "relevance": None,
            "best_direction": None,
            "reason": reason.strip(),
        }
    # Legacy callers supply only score. A present but invalid relevance never falls back.
    relevance = _valid_score(item.get("relevance", item.get("score")), "relevance")
    if "score" in item:
        _valid_score(item["score"], "score")
    direction = item.get("best_direction")
    if direction == "":
        direction = None
    if direction is not None and (
        not isinstance(direction, str) or direction not in directions
    ):
        raise ValueError("best_direction must be an enabled direction ID or null")
    return {
        **item,
        "status": "scored",
        "score": relevance,
        "relevance": relevance,
        "best_direction": direction,
        "reason": reason.strip(),
    }


async def score_papers_batch(
    llm_client: LLMClient | None,
    papers: list,
    batch_size: int = 10,
) -> tuple[list[dict], list[tuple]]:
    """Return valid relevance assessments and (batch offset, retryable count, error).

    A duplicate index invalidates all its assessments. Missing or invalid papers are
    never returned as scored; valid siblings remain usable. No database writes occur.
    """
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or batch_size <= 0
    ):
        raise ValueError("batch_size must be a positive integer")
    if not papers:
        return [], []
    directions, system_template, user_template, fingerprint = _scoring_context()
    if not directions:
        raise ValueError("No enabled research directions; nothing can be screened")
    system_prompt = system_template.format(
        directions_desc=_build_directions_desc(directions)
    )
    provenance = {
        "schema_version": 2,
        "fingerprint": fingerprint,
        "evidence_scope": "title_abstract",
    }
    all_results = []
    failed_batches = []

    for batch_start in range(0, len(papers), batch_size):
        batch = papers[batch_start : batch_start + batch_size]
        eligible = {
            index: paper
            for index, paper in enumerate(batch)
            if has_screening_material(paper)
        }
        valid = []
        for index, paper in enumerate(batch):
            if index not in eligible:
                valid.append(
                    {
                        "index": index,
                        "status": "pending_needs_material",
                        "score": None,
                        "relevance": None,
                        "best_direction": None,
                        "reason": "材料不足：缺少可用研究摘要，需补全正文或摘要后再评估",
                        "_paper": paper,
                        "_batch_offset": batch_start,
                        "_provenance": {
                            **provenance,
                            "status": "pending_needs_material",
                            "evidence_fingerprint": evidence_fingerprint(paper),
                        },
                    }
                )
        if not eligible:
            all_results.extend(valid)
            continue
        user_prompt = user_template.format(
            papers_text=_format_papers_for_llm(list(eligible.values()), eligible),
            direction_ids=", ".join(directions),
        )
        try:
            response = await llm_client.chat_json(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
                max_tokens=4000,
            )
            if isinstance(response, dict):
                response = response.get("papers", response.get("results"))
            if not isinstance(response, list):
                raise ValueError("Expected an assessment list or a papers/results list")

            indexed = {}
            errors = []
            for item in response:
                index = item.get("index") if isinstance(item, dict) else None
                if type(index) is not int or index not in eligible:
                    errors.append("Invalid or out-of-range paper index")
                    continue
                indexed.setdefault(index, []).append(item)

            for index in eligible:
                items = indexed.get(index, [])
                if len(items) != 1:
                    errors.append(
                        f"Paper {index}: {'duplicate' if items else 'missing'} index"
                    )
                    continue
                try:
                    item = _normalize_assessment(items[0], directions)
                except ValueError as error:
                    errors.append(f"Paper {index}: {error}")
                    continue
                item.update(
                    _paper=batch[index],
                    _batch_offset=batch_start,
                    _provenance={
                        **provenance,
                        "status": item["status"],
                        "evidence_fingerprint": evidence_fingerprint(batch[index]),
                        **(
                            {"direction_scores": item["direction_scores"]}
                            if "direction_scores" in item
                            else {}
                        ),
                    },
                )
                valid.append(item)
            all_results.extend(sorted(valid, key=lambda item: item["index"]))
            pending = len(batch) - len(valid)
            if pending:
                failed_batches.append((batch_start, pending, "; ".join(errors)[:200]))
            elif errors:
                logger.warning(
                    "Ignored invalid extra assessments: %s", "; ".join(errors)
                )
            logger.info(
                "Scored %s/%s papers in batch at %s",
                len(valid),
                len(batch),
                batch_start,
            )
        except Exception as error:
            logger.error("Batch scoring failed; papers remain retryable: %s", error)
            all_results.extend(valid)
            failed_batches.append((batch_start, len(eligible), str(error)[:200]))
    return all_results, failed_batches


def apply_scores(
    results: list[dict],
    threshold: float | None = None,
    dry_run: bool = False,
) -> dict:
    """Persist relevance, using only the configured/explicit threshold for eligibility."""
    config = _load_scoring_config()
    directions = _enabled_directions(config)
    threshold = (
        _configured_threshold(config)
        if threshold is None
        else _valid_score(threshold, "threshold")
    )
    stats = {
        "total_processed": len(results),
        "total_scored": 0,
        "selected": 0,
        "rejected": 0,
        "pending_needs_material": 0,
        "by_direction": {},
        "errors": 0,
    }
    session = None
    try:
        if not dry_run:
            init_db()
            session = get_session_factory()()
        repo = ReportRepository(session) if session is not None else None
        for raw_item in results:
            try:
                item = _normalize_assessment(raw_item, directions)
                paper = item.get("_paper")
                if paper is None or _paper_value(paper, "id", None) is None:
                    raise ValueError("Assessment has no persisted paper ID")
                paper_id = _paper_value(paper, "id")
                if not has_screening_material(paper):
                    item.update(
                        status="pending_needs_material",
                        score=None,
                        relevance=None,
                        best_direction=None,
                        reason="材料不足：缺少可用研究摘要，需补全正文或摘要后再评估",
                    )
                relevance = item["relevance"]
                pending = item["status"] == "pending_needs_material"
                is_selected = not pending and relevance >= threshold
                direction = item["best_direction"]
                # Direction and screening scope/status already have structured fields.
                reason = relevance_reason_text(item["reason"])
                if dry_run:
                    logger.info(
                        "[%s] %s | %s",
                        relevance,
                        item["status"],
                        _paper_value(paper, "title"),
                    )
                else:
                    provenance = {
                        **(
                            item.get("_provenance")
                            or {
                                "schema_version": 2,
                                "fingerprint": scoring_fingerprint(),
                                "evidence_scope": "title_abstract",
                            }
                        ),
                        "status": item["status"],
                        "evidence_fingerprint": evidence_fingerprint(paper),
                    }
                    from reportagent.models.database import Report

                    if session.bind.dialect.name == "sqlite":
                        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                    session.expire_all()
                    current = session.get(Report, paper_id)
                    if current is None:
                        raise ValueError(f"Paper {paper_id} no longer exists")
                    if item.get("_provenance") and (
                        provenance.get("fingerprint") != scoring_fingerprint()
                        or provenance["evidence_fingerprint"]
                        != evidence_fingerprint(current)
                    ):
                        raise ValueError(
                            "Research material or relevance policy changed during scoring"
                        )
                    if "direction_scores" in item:
                        provenance["direction_scores"] = item["direction_scores"]
                    if not repo.update_selection(
                        paper_id,
                        0 if pending else relevance,
                        reason,
                        is_selected,
                        direction=direction,
                        relevance=relevance,
                        provenance_json=json.dumps(provenance, ensure_ascii=False),
                        commit=False,
                    ):
                        raise ValueError(f"Paper {paper_id} no longer exists")
                    session.commit()
                if pending:
                    stats["pending_needs_material"] += 1
                    continue
                stats["total_scored"] += 1
                if is_selected:
                    stats["selected"] += 1
                    if direction is not None:
                        stats["by_direction"][direction] = (
                            stats["by_direction"].get(direction, 0) + 1
                        )
                else:
                    stats["rejected"] += 1
            except (ValueError, TypeError, KeyError) as error:
                if session is not None:
                    session.rollback()
                logger.warning("Assessment left unjudged: %s", error)
                stats["errors"] += 1
            except Exception as error:
                if session is not None:
                    session.rollback()
                logger.error("Failed to persist assessment: %s", error)
                stats["errors"] += 1
    finally:
        if session is not None:
            session.close()
    return stats


async def score_reports(options: ScoringOptions) -> None:
    init_db()
    factory = get_session_factory()
    session = factory()

    # ── Load papers ──
    since = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if options.days > 1:
        since = since - timedelta(days=options.days - 1)

    try:
        repo = ReportRepository(session)
        fingerprint = scoring_fingerprint()
        directions = load_enabled_directions()
        threshold = (
            _configured_threshold(_load_scoring_config())
            if options.threshold is None
            else _valid_score(options.threshold, "threshold")
        )

        if getattr(options, "published_days", None):
            published_since = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            ) - timedelta(days=options.published_days - 1)
            papers = repo.get_unjudged_reports(
                published_since=published_since,
                limit=options.max_papers,
                scoring_fingerprint=fingerprint,
            )
        elif options.all_unjudged:
            papers = repo.get_unjudged_reports(
                limit=options.max_papers, scoring_fingerprint=fingerprint
            )
        else:
            papers = repo.get_unjudged_reports(
                since=since, limit=options.max_papers, scoring_fingerprint=fingerprint
            )

        logger.info(
            f"Found {len(papers)} unjudged or stale papers"
            f"{' since ' + since.strftime('%Y-%m-%d') if not options.all_unjudged else ' (all)'}"
        )

        if not papers:
            logger.info("No papers to score. Done.")
            return

        # ── Init LLM ──
        try:
            llm_client = (
                LLMClient()
                if any(has_screening_material(paper) for paper in papers)
                else None
            )
            if llm_client is not None:
                logger.info(
                    f"LLM client ready: provider={llm_client.provider}, model={llm_client.model}"
                )
        except Exception as e:
            logger.error(f"LLM client init failed: {e}")
            raise

        # ── Score ──
        logger.info(
            f"Scoring {len(papers)} papers in batches of {options.batch_size}..."
        )
        results, failed_batches = await score_papers_batch(
            llm_client, papers, batch_size=options.batch_size
        )

        # ── Apply ──
        if options.dry_run:
            logger.info(f"\n{'=' * 50}")
            logger.info("DRY RUN — Preview of LLM scoring results:")
            logger.info(f"{'=' * 50}")

        stats = apply_scores(results, threshold=threshold, dry_run=options.dry_run)

        # ── Summary ──
        logger.info(f"\n{'=' * 50}")
        logger.info("LLM Filter Summary:")
        logger.info(f"  Total scored : {stats['total_scored']}")
        logger.info(f"  ⭐ Selected  : {stats['selected']} (score >= {threshold})")
        logger.info(f"  ✗ Rejected   : {stats['rejected']}")
        logger.info(f"  Pending material: {stats['pending_needs_material']}")
        if stats["errors"]:
            logger.info(f"  ⚠ Errors    : {stats['errors']}")
        if stats["by_direction"]:
            logger.info("  By direction:")
            for d_id, count in sorted(
                stats["by_direction"].items(), key=lambda x: -x[1]
            ):
                d_name = directions.get(d_id, {}).get("name", d_id)
                logger.info(f"    {d_name}: {count} 篇")
        logger.info(f"{'=' * 50}")

        if options.dry_run:
            logger.info("[DRY RUN] No changes written to database.")

        # Persist valid siblings, but make partial failure visible to callers.
        if stats["errors"]:
            raise RuntimeError(
                f"Failed to persist {stats['errors']} relevance assessment(s); "
                "affected papers remain retryable."
            )
        if failed_batches:
            total_failed = sum(n for _, n, _ in failed_batches)
            raise RuntimeError(
                f"LLM scoring failed for {total_failed} paper(s) in "
                f"{len(failed_batches)} batch(es); they remain retryable. "
                f"First error: {failed_batches[0][2]}"
            )

    finally:
        session.close()
