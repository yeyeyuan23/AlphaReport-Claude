"""Incremental startup ingestion, using the same PDF parser/classifier/repository as collection."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import logging
from pathlib import Path

from reportagent.classifiers.llm_classifier import LLMClassifier
from reportagent.classifiers.rule_classifier import RuleClassifier
from reportagent.db.engine import get_session_factory, init_db
from reportagent.db.repository import ReportRepository
from reportagent.llm.client import LLMClient
from reportagent.models.database import LocalPDFImport, Report
from reportagent.models.schemas import UserCriteria
from reportagent.services.collection import classify_for_storage
from reportagent.sources.base import SourceParseError, safe_source_error
from reportagent.sources.local_pdf import LocalPDFSource
from reportagent.utils.config import PROJECT_ROOT, get_config, get_db_path

logger = logging.getLogger(__name__)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _scan(root: Path, result: dict):
    files = await asyncio.to_thread(
        lambda: sorted(
            p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"
        )
    )
    result["scanned"] = len(files)
    source = LocalPDFSource(str(root))
    criteria = UserCriteria(topics=["量化金融"])
    rules = RuleClassifier()
    factory = get_session_factory()
    llm = None
    llm_initialized = False
    for path in files:
        file_path = str(path.resolve())
        try:
            fingerprint = await asyncio.to_thread(_file_hash, path)
            with factory() as session:
                known = session.get(LocalPDFImport, file_path)
                known_report = (
                    session.get(Report, known.report_id)
                    if known and known.report_id
                    else None
                )
                if (
                    known
                    and known.file_sha256 == fingerprint
                    and (
                        known.status == "filtered"
                        or (
                            known_report is not None
                            and known_report.content_hash == known.report_content_hash
                        )
                    )
                ):
                    result["unchanged"] += 1
                    continue
            sr = await asyncio.to_thread(source._process_pdf, path, criteria)
            if sr is None:
                raise SourceParseError("PDF produced no research material")
            if not llm_initialized:
                llm_initialized = True
                try:
                    llm = LLMClassifier(LLMClient())
                except Exception as error:
                    logger.warning(
                        "Local PDF %s",
                        safe_source_error(error, "classification initialization"),
                    )
            errors = []
            classified = await classify_for_storage(sr, rules, llm, errors)
            if await asyncio.to_thread(_file_hash, path) != fingerprint:
                raise SourceParseError(
                    "PDF changed during import; retry on next startup"
                )
            with factory() as session:
                if classified is None:
                    session.merge(
                        LocalPDFImport(
                            file_path=file_path,
                            file_sha256=fingerprint,
                            status="filtered",
                        )
                    )
                    session.commit()
                    result["filtered"] += 1
                    continue
                action, report_id = ReportRepository(session).upsert_report(classified)
                result[action] += 1
                result["report_ids"].append(report_id)
                if llm is None:
                    errors.append("classification unavailable; rule fallback used")
                if not errors:
                    session.merge(
                        LocalPDFImport(
                            file_path=file_path,
                            file_sha256=fingerprint,
                            report_id=report_id,
                            report_content_hash=session.get(
                                Report, report_id
                            ).content_hash,
                            status="imported",
                        )
                    )
                    session.commit()
            if errors:
                result["failed"] += 1
                result["errors"].extend(errors)
                logger.warning(
                    "Local PDF %s: classification incomplete; retry on next startup",
                    path.name,
                )
        except Exception as error:
            result["failed"] += 1
            message = safe_source_error(error, "local PDF import")
            result["errors"].append(message)
            logger.warning("Local PDF %s: %s", path.name, message)


async def import_pdf_library() -> dict:
    """Import new/changed files in the background; never make service startup fail."""
    result = {
        "status": "ok",
        "scanned": 0,
        "unchanged": 0,
        "added": 0,
        "updated": 0,
        "filtered": 0,
        "failed": 0,
        "errors": [],
        "report_ids": [],
    }
    config = get_config("sources", "local_pdf", default={})
    if not config.get("enabled", True) or not config.get("import_on_startup", True):
        return {**result, "status": "disabled"}
    try:
        root = (PROJECT_ROOT / config.get("library_path", "data/pdf_library")).resolve()
        root.mkdir(parents=True, exist_ok=True)
        init_db()
        # Shared across Web workers. A second startup must not duplicate model calls.
        lock_path = get_db_path().with_suffix(".pdf-import.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {**result, "status": "already_running"}
            try:
                await _scan(root, result)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        if result["failed"]:
            result["status"] = "partial"
    except Exception as error:
        result["status"] = "failed"
        result["errors"].append(safe_source_error(error, "local PDF library"))
        logger.exception("Local PDF startup import failed")
    logger.info("Local PDF startup import: %s", result)
    return result
