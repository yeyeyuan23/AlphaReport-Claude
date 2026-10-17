"""Every local import entry discovers the same case-insensitive PDF inventory."""

import asyncio

from reportagent.models.database import LocalPDFImport
from reportagent.models.schemas import SearchResult, SourceType, UserCriteria
from reportagent.services import local_pdf_import as startup
from reportagent.sources.local_pdf import LocalPDFSource


def test_startup_and_source_search_share_uppercase_and_nested_pdf_inventory(
    tmp_path, monkeypatch, memory_session_factory
):
    paths = [tmp_path / "paper.PDF", tmp_path / "nested" / "second.pdf"]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4 fixture")
    (tmp_path / "ignore.pdf.bak").write_text("not a PDF")
    (tmp_path / "directory.pdf").mkdir()
    factory = memory_session_factory
    with factory() as db:
        for path in paths:
            db.add(
                LocalPDFImport(
                    file_path=str(path.resolve()),
                    file_sha256=startup._file_hash(path),
                    status="filtered",
                )
            )
        db.commit()
    monkeypatch.setattr(startup, "get_session_factory", lambda: factory)
    scanned = {"unchanged": 0}
    asyncio.run(startup._scan(tmp_path, scanned))
    assert scanned == {"scanned": 2, "unchanged": 2}
    observed = []

    def process(self, path, criteria):
        observed.append(path)
        return SearchResult(
            title=path.stem, source=SourceType.LOCAL_PDF, source_url=str(path)
        )

    monkeypatch.setattr(LocalPDFSource, "_process_pdf", process)
    results = asyncio.run(
        LocalPDFSource(str(tmp_path)).search(UserCriteria(topics=["portfolio"]))
    )
    assert set(observed) == set(paths)
    assert len(results) == 2
