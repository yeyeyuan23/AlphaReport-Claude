"""Maintenance parsing with guarded, atomic replacement of PDF material."""

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import update

from reportagent.models.database import Report
from reportagent.services import report_pdf
from reportagent.services.pdf_extraction import extract_formulas_mineru
from reportagent.sources import content_fetch
from reportagent.sources.base import SourceParseError, usable_full_text


def material_snapshot(report):
    """Guard source, evidence and outputs that this maintenance run may replace."""
    return {
        **report_pdf.pdf_snapshot(report),
        **{
            field: getattr(report, field)
            for field in (
                "title",
                "abstract",
                "full_text",
                "has_full_text",
                "tables_json",
                "equations_json",
                "summary",
                "analysis_json",
                "evaluation_json",
                "knowledge_card_json",
            )
        },
    }


def _material_values(snapshot, path, tables, equations, full_text):
    values = {
        "full_text": full_text,
        "has_full_text": True,
        "pdf_path": path,
        "tables_json": json.dumps(tables, ensure_ascii=False),
        "equations_json": json.dumps(equations, ensure_ascii=False),
    }
    if any(values[field] != snapshot[field] for field in values):
        values.update(
            summary=None,
            analysis_json=None,
            evaluation_json=None,
            knowledge_card_json=None,
        )
    return values


def _save_cached_material(db, snapshot, values):
    try:
        # Reload in this session instead of mutating a detached earlier object.
        db.expire_all()
        current = db.get(Report, snapshot["id"], populate_existing=True)
        if current is None or material_snapshot(current) != snapshot:
            raise SourceParseError("Report material changed during PDF parsing")
        with db.no_autoflush:
            result = db.execute(
                update(Report)
                .where(
                    *(getattr(Report, key) == value for key, value in snapshot.items())
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
        if result.rowcount != 1:
            raise SourceParseError("Report material changed during PDF parsing")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    db.expire_all()


def _file_fingerprint(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def parse_report_pdf(db, report_id, *, save=False, directory=None, parser=None):
    """Parse existing or safely downloaded PDF; failed parses never change the row."""
    if type(report_id) is not int or report_id <= 0:
        raise ValueError("report_id must be a positive integer")
    report = db.get(Report, report_id, populate_existing=True)
    if report is None:
        raise SourceParseError(f"Report {report_id} not found")
    snapshot = material_snapshot(report)
    path = report_pdf.cached_pdf_path(report)
    data = None
    if path is None:
        if report.source != "arxiv" or not (report.arxiv_id or report.source_url):
            raise SourceParseError("No PDF available for this report")
        data = report_pdf.download_pdf_sync(snapshot, content_fetch.get_pdf_policy())
    parse = parser or extract_formulas_mineru
    with TemporaryDirectory() as temporary:
        if data is not None:
            path = str(Path(temporary) / "report.pdf")
            Path(path).write_bytes(data)
        fingerprint = _file_fingerprint(path)
        tables, equations, text = parse(path)
        if _file_fingerprint(path) != fingerprint:
            raise SourceParseError("PDF file changed during parsing")
        if not usable_full_text(text):
            raise SourceParseError("PDF has no usable extracted text")
        if equations:
            from reportagent.processors.formula_normalizer import normalize_formulas

            equations = normalize_formulas(equations)
        if data is not None:
            values = (
                _material_values(snapshot, path, tables, equations, text)
                if save
                else None
            )
            # save_pdf overrides the temporary path with the atomically promoted
            # cache location and compares the complete original material snapshot.
            path = report_pdf.save_pdf(
                db,
                snapshot,
                data,
                Path(directory)
                if directory is not None
                else content_fetch.PDF_DIRECTORY,
                values=values,
            )
        elif save:
            _save_cached_material(
                db, snapshot, _material_values(snapshot, path, tables, equations, text)
            )
    return path, tables, equations, text
