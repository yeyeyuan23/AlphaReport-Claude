from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import fitz


@dataclass
class PDFContent:
    text: str
    page_count: int
    metadata: dict = field(default_factory=dict)
    file_path: str = ""
    file_size_bytes: int = 0
    first_page_text: str = ""


class PDFExtractor:
    def extract(self, pdf_path: str | Path) -> PDFContent:
        pdf_path = Path(pdf_path)
        with fitz.open(str(pdf_path)) as document:
            return self._extract(document, str(pdf_path), pdf_path.stat().st_size)

    def extract_bytes(self, data: bytes) -> PDFContent:
        with fitz.open(stream=data, filetype="pdf") as document:
            return self._extract(document, "", len(data))

    @staticmethod
    def _extract(document, file_path, file_size_bytes) -> PDFContent:
        text_parts = [page.get_text() for page in document]
        meta = document.metadata or {}
        return PDFContent(
            text="\n".join(text_parts),
            page_count=document.page_count,
            metadata={
                "title": meta.get("title", ""),
                "author": meta.get("author", ""),
                "subject": meta.get("subject", ""),
                "creator": meta.get("creator", ""),
                "creation_date": meta.get("creationDate", ""),
            },
            file_path=file_path,
            file_size_bytes=file_size_bytes,
            first_page_text=text_parts[0] if text_parts else "",
        )

    def batch_extract(self, directory: str | Path) -> list[PDFContent]:
        directory = Path(directory)
        results = []
        for pdf_file in sorted(directory.rglob("*.pdf")):
            try:
                results.append(self.extract(pdf_file))
            except Exception:
                pass
        return results
