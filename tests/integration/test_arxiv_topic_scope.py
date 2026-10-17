"""Exercise configured scope through real dated-list and Atom parsers."""

import asyncio
from datetime import datetime, timezone

import httpx

from reportagent.sources import factory
from reportagent.models.schemas import UserCriteria
from tests.support.source_fixtures import mock_http, atom_entry, atom_feed
from reportagent.utils import config


def listing(category, identifiers):
    count = len(identifiers)
    rows = "".join(
        f'<dt><a title="Abstract" id="{i}" href="/abs/{i}">{i}</a></dt>'
        for i in identifiers
    )
    return f'<div id="dlpage"><div class="paging">Total of {count} entries</div><ul><li><a href="/list/{category}/recent?skip=0">Fri, 16 Oct 2026</a></li></ul><dl id="articles"><h3>Fri, 16 Oct 2026 (showing {count} of {count} entries)</h3>{rows}</dl></div>'


def test_configured_sources_keep_all_qfin_add_topic_matches_and_deduplicate(
    monkeypatch,
):
    scope = {
        "full_categories": ["q-fin"],
        "topic_groups": [
            {
                "name": "ml",
                "categories": ["cs.LG", "stat.ML"],
                "keywords": ["time series"],
            },
            {
                "name": "optimization",
                "categories": ["math.OC"],
                "keywords": ["optimal execution"],
            },
        ],
    }
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: {
            "sources": {
                "arxiv": {
                    "search_scope": scope,
                    "rate_limit_seconds": 0,
                    "download_pdfs": False,
                }
            }
        },
    )
    qfin = "2610.00000"
    ml = [f"2610.{i:05d}" for i in range(1, 123)]
    unrelated = "2610.00123"
    optimization = "2610.00124"
    categories = {
        "q-fin": [qfin],
        "cs.LG": [qfin, *ml, unrelated],
        "stat.ML": ml[:3],
        "math.OC": [optimization],
    }
    batches, paths = [], []

    def respond(request):
        paths.append(request.url.path)
        if request.url.host == "arxiv.org":
            category = request.url.path.split("/")[2]
            return httpx.Response(200, text=listing(category, categories[category]))
        identifiers = request.url.params["id_list"].split(",")
        batches.append(len(identifiers))
        rows = []
        for i in identifiers:
            title = (
                "TIME-SERIES forecasting"
                if i in ml
                else "Optimal execution"
                if i == optimization
                else "A general theorem"
            )
            row = (
                atom_entry(i + "v1")
                .replace("Portfolio optimization quantitative risk model", title)
                .replace(
                    "A finance portfolio risk model with empirical backtests.",
                    "A general method without finance words.",
                )
            )
            rows.append(row)
        return httpx.Response(200, text=atom_feed("".join(rows), len(rows)))

    mock_http(monkeypatch, respond)
    source = factory.build_sources(["arxiv"])[0]
    rows = asyncio.run(
        source.search_broad(
            UserCriteria(
                topics=[],
                date_basis="announcement",
                date_from=datetime(2026, 10, 16, tzinfo=timezone.utc),
                date_to=datetime(2026, 10, 17, tzinfo=timezone.utc),
                date_to_exclusive=True,
                max_results_per_source=None,
            )
        )
    )
    assert {r.arxiv_id for r in rows} == {i + "v1" for i in [qfin, *ml, optimization]}
    assert batches == [100, 25]
    assert source.last_search.errors == []
    assert source.discovery_stats["unique_announcements"] == 125
    assert source.discovery_stats["selected"] == 124
    assert source.discovery_stats["added_by_topics"] == 123
    assert set(source.discovery_stats["categories"]) == set(categories)
    assert all(
        r.announced_at == datetime(2026, 10, 16, tzinfo=timezone.utc) for r in rows
    )
    assert not any("/pdf/" in path for path in paths)
    cross = next(r for r in rows if r.arxiv_id == qfin + "v1")
    assert cross.raw_metadata["announcement_info"]["listed_categories"] == [
        "q-fin",
        "cs.LG",
    ]


def test_candidate_cli_only_reads_metadata_and_writes_requested_output(
    monkeypatch, tmp_path, capsys
):
    import json
    import sys
    from reportagent.cli import arxiv_candidates
    from tests.integration.test_arxiv_announcements import recorded

    monkeypatch.setattr(
        config,
        "load_config",
        lambda: {
            "sources": {"arxiv": {"rate_limit_seconds": 0, "download_pdfs": True}}
        },
    )

    def respond(request):
        if request.url.host == "arxiv.org":
            assert request.url.path == "/list/q-fin/recent"
            return httpx.Response(200, content=recorded("q-fin-recent.html"))
        assert "id_list" in request.url.params
        return httpx.Response(200, content=recorded("q-fin-metadata.xml"))

    mock_http(monkeypatch, respond)
    output = tmp_path / "candidate-count.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["arxiv_candidates", "--report-date", "2026-10-17", "--output", str(output)],
    )
    assert arxiv_candidates.main() == 0
    receipt = json.loads(output.read_text())
    assert receipt["candidate_count"] == 17
    assert receipt["status"] == "ok"
    assert len(receipt["candidates"]) == 17
    assert receipt["publication_window"] == {
        "start": "2026-10-16T00:00:00+00:00",
        "end": "2026-10-17T00:00:00+00:00",
        "end_exclusive": True,
    }
    assert json.loads(capsys.readouterr().out)["candidate_count"] == 17
