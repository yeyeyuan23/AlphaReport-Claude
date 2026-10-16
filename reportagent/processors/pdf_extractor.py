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


class PDFExtractor:
    def extract(self, pdf_path: str | Path) -> PDFContent:
        pdf_path = Path(pdf_path)
        doc = fitz.open(str(pdf_path))
        try:
            text_parts = []
            for page in doc:
                text_parts.append(page.get_text())
            text = "\n".join(text_parts)

            meta = doc.metadata or {}
            return PDFContent(
                text=text,
                page_count=doc.page_count,
                metadata={
                    "title": meta.get("title", ""),
                    "author": meta.get("author", ""),
                    "subject": meta.get("subject", ""),
                    "creator": meta.get("creator", ""),
                    "creation_date": meta.get("creationDate", ""),
                },
                file_path=str(pdf_path),
                file_size_bytes=pdf_path.stat().st_size,
            )
        finally:
            doc.close()

    def batch_extract(self, directory: str | Path) -> list[PDFContent]:
        directory = Path(directory)
        results = []
        for pdf_file in sorted(directory.rglob("*.pdf")):
            try:
                results.append(self.extract(pdf_file))
            except Exception:
                pass
        return results
