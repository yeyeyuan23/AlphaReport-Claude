"""PDF extraction shared by command adapters and other callers."""
import logging

logger = logging.getLogger(__name__)


def extract_text_pymupdf(pdf_path: str) -> str:
    """Extract text using PyMuPDF (fast, no LLM needed)."""
    import fitz
    doc = fitz.open(pdf_path)
    try:
        pages = []
        for page in doc:
            pages.append(page.get_text())
        return "\n\n".join(pages)
    finally:
        doc.close()


def extract_formulas_mineru(pdf_path: str) -> tuple[list, list, str]:
    """Extract formulas and tables using MinerU if available."""
    try:
        from reportagent.processors.mineru_parser import MinerUParser, _run_mineru_parse
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            content_list = _run_mineru_parse(pdf_path, tmp_dir)
            parser = MinerUParser()
            result = parser._build_result(content_list, pdf_path)
            return result.tables, result.equations, result.full_text_with_tables
    except ImportError:
        logger.warning("MinerU not available, falling back to PyMuPDF")
        text = extract_text_pymupdf(pdf_path)
        return [], [], text
    except Exception as e:
        logger.warning(f"MinerU parse failed: {e}, falling back to PyMuPDF")
        text = extract_text_pymupdf(pdf_path)
        return [], [], text
