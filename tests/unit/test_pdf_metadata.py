from datetime import datetime

import pytest

from reportagent.processors.metadata_extractor import MetadataExtractor
from reportagent.processors.pdf_extractor import PDFContent


def content(text, **metadata):
    return PDFContent(text=text, page_count=1, metadata=metadata)


@pytest.mark.parametrize(
    "line",
    [
        "arXiv: 2501.12345v2",
        "https://arxiv.org/abs/2501.12345v2",
        "https://arxiv.org/pdf/2501.12345v2.pdf",
        "2501.12345v2",
    ],
)
def test_explicit_arxiv_front_matter_forms_keep_the_paper_identity(line):
    result = MetadataExtractor().extract(content("Verified paper title\n" + line))
    assert result["arxiv_id"] == "2501.12345"


@pytest.mark.parametrize(
    "line", ["DOI: 10.1234/Paper", "https://doi.org/10.1234/Paper", "10.1234/Paper"]
)
def test_explicit_doi_front_matter_forms_keep_the_paper_identity(line):
    result = MetadataExtractor().extract(content("Verified paper title\n" + line))
    assert result["doi"] == "10.1234/paper"


@pytest.mark.parametrize(
    "prose",
    [
        "We compare arXiv: 2501.12345 and doi: 10.1234/cited.",
        "Prior work: https://arxiv.org/abs/2501.12345, https://doi.org/10.1234/cited.",
    ],
)
def test_unheaded_first_page_prose_does_not_supply_identity(prose):
    result = MetadataExtractor().extract(content("Verified paper title\n" + prose))
    assert result["arxiv_id"] is None
    assert result["doi"] is None


def test_pdf_creation_time_is_not_a_publication_date():
    result = MetadataExtractor().extract(
        content(
            "Stock prediction paper\nAbstract\nWe predict stock returns.\n1 Introduction\nDetails.",
            creation_date="D:20260121123000+08'00'",
        )
    )
    assert result["date"] is None


@pytest.mark.parametrize(
    "label",
    [
        "Published: 2024-02-03",
        "Publication date: 3 February 2024",
        "发表日期：2024年2月3日",
    ],
)
def test_only_explicit_publication_date_labels_are_accepted(label):
    result = MetadataExtractor().extract(
        content(
            "Stock prediction paper\n" + label + "\nAbstract\nWe predict returns.",
            creation_date="D:20260102123000",
        )
    )
    assert result["date"] == datetime(2024, 2, 3)


def test_received_dates_and_cited_publication_dates_are_not_used():
    result = MetadataExtractor().extract(
        content(
            "Stock prediction paper\nReceived: 2024-02-03\n"
            "Abstract\nWe use a model published in 2023-01-01.\n1 Introduction\nText.\n"
            "References\nPublished: 2020-01-01"
        )
    )
    assert result["date"] is None


def test_explicit_publication_metadata_wins_over_file_creation_time():
    result = MetadataExtractor().extract(
        content(
            "Stock prediction paper",
            publication_date="2024-02-03",
            creation_date="D:20260102123000",
        )
    )
    assert result["date"] == datetime(2024, 2, 3)


@pytest.mark.parametrize(
    "imprint,year",
    [
        (
            "Copyright © 2024, Association for the Advancement of Artificial Intelligence.\n(AAAI-24)",
            2024,
        ),
        (
            "Proceedings of the Thirty-First International Joint Conference on Artificial Intelligence (IJCAI-22)",
            2022,
        ),
        ("KDD ’23, August 6–10, 2023, Long Beach, CA, USA", 2023),
        ("Published as a conference paper at ICLR 2025", 2025),
        (
            "Proceedings of the 2024 Conference on Empirical Methods in Natural Language Processing",
            2024,
        ),
        ("ICML ’24, July 21–27, 2024, Vienna", 2024),
    ],
)
def test_publication_imprint_preserves_year_without_inventing_a_day(imprint, year):
    result = MetadataExtractor().extract(
        content("Paper title\nAbstract\nThe abstract.\n" + imprint)
    )
    assert result["date"] is None
    assert result["publication_date_info"]["year"] == year
    assert result["publication_date_info"]["precision"] == "year"
    assert result["publication_date_info"]["evidence"]


def test_arxiv_revision_stamp_is_preserved_separately_from_conference_year():
    result = MetadataExtractor().extract(
        content(
            "Paper title\nABSTRACT\nThe abstract.\n"
            "KDD ’21, August 14–18, 2021, Singapore\n"
            "arXiv:2106.12950v2  [cs.LG]  25 Jun 2021"
        )
    )
    assert result["date"] is None
    info = result["publication_date_info"]
    assert info["year"] == 2021 and info["precision"] == "year"
    assert info["arxiv_version_date"] == "2021-06-25" and info["arxiv_version"] == "v2"


def test_explicit_publication_month_is_not_lost_or_rounded_to_day_one():
    result = MetadataExtractor().extract(
        content("Paper title\nPublished: August 2023\nAbstract\nThe abstract.")
    )
    assert result["date"] is None
    assert result["publication_date_info"]["precision"] == "month"
    assert result["publication_date_info"]["year"] == 2023
    assert result["publication_date_info"]["month"] == 8


def test_a_conference_mentioned_in_research_prose_is_not_this_papers_date():
    result = MetadataExtractor().extract(
        content(
            "Paper title\nAbstract\nWe compare the model presented at AAAI-23.\n"
            "1 Introduction\nThis paper studies financial forecasting."
        )
    )
    assert result["date"] is None
    assert result["publication_date_info"]["precision"] == "unknown"


def test_actual_abstract_is_complete_and_excludes_front_matter_and_intro():
    from reportagent.processors.paper_sections import extract_paper_sections

    abstract = (
        "We predict stock returns with financial factors. " * 60 + "TAIL_EVIDENCE."
    )
    result = extract_paper_sections(
        "Title\nAuthors and affiliations\nAbstract\n"
        + abstract
        + "\nKeywords: stock prediction\n1 Introduction\nThe motivation and context.\n2 Related Work\nOther work."
    )
    assert result["abstract"] == abstract
    assert result["introduction"] == "The motivation and context."


@pytest.mark.parametrize("heading", ["ABSTRACT", "Abstract—", "## Abstract", "摘 要："])
def test_abstract_heading_styles(heading):
    from reportagent.processors.paper_sections import extract_paper_sections

    result = extract_paper_sections(
        heading
        + "\nThe actual abstract.\n1\nINTRODUCTION\nThe intro.\n2\nMETHOD\nMethods."
    )
    assert result["abstract"] == "The actual abstract."
    assert result["introduction"] == "The intro."


def test_no_section_heading_does_not_invent_an_abstract_from_first_chars():
    from reportagent.processors.paper_sections import extract_paper_sections

    assert extract_paper_sections(
        "Title\nAuthor list\nBody without recognizable sections."
    ) == {"abstract": None, "introduction": None}


def test_toc_entry_is_not_mistaken_for_abstract():
    from reportagent.processors.paper_sections import extract_paper_sections

    result = extract_paper_sections(
        "Contents\nAbstract ........ 1\n1 Introduction .... 2\n"
        "Abstract\nThe actual abstract.\n1 Introduction\nActual intro.\n2 Methods\nMethods."
    )
    assert result["abstract"] == "The actual abstract."


def test_author_footnotes_and_copyright_are_not_abstract_content():
    from reportagent.processors.paper_sections import extract_paper_sections

    result = extract_paper_sections(
        "ABSTRACT\nThe actual research abstract.\n"
        "∗The first two authors contributed equally.\nPermission to make digital copies.\n"
        "© 2021 ACM\n1 INTRODUCTION\nThe intro.\n2 METHODS\nMethods."
    )
    assert result["abstract"] == "The actual research abstract."


def test_wrapped_numeric_prose_and_section_name_words_do_not_end_sections():
    from reportagent.processors.paper_sections import extract_paper_sections

    abstract = "We evaluate our portfolio on\n500 stocks over ten years and find predictive gains."
    intro = (
        "Taking two earnings calls in July\n2017 from Amazon and Microsoft as an example, we first\n"
        "compare prior research. These\nmethods usually adopt pipeline architecture and two modalities."
    )
    result = extract_paper_sections(
        "Abstract\n"
        + abstract
        + "\n1 Introduction\n"
        + intro
        + "\n2 Our Framework\nThe method section."
    )
    assert result == {"abstract": abstract, "introduction": intro}


def test_sentence_case_numbered_heading_ends_introduction():
    from reportagent.processors.paper_sections import extract_paper_sections

    result = extract_paper_sections(
        "1 Introduction\nOur motivation.\n2 Related work\nOther studies.\n3 Methods\nThe method."
    )
    assert result["introduction"] == "Our motivation."


@pytest.mark.parametrize(
    "reference_header", ["# References", "## 5 References", "5. References"]
)
def test_markdown_or_numbered_reference_dates_never_become_publication_dates(
    reference_header,
):
    result = MetadataExtractor().extract(
        content(
            "Title\n# Abstract\nWe study returns.\n"
            + reference_header
            + "\nPublished: 2020-01-01\nKDD ’20, August 23–27, 2020, Virtual conference"
        )
    )
    assert result["date"] is None
    assert result["publication_date_info"]["precision"] == "unknown"


def test_local_pdf_uses_intro_for_classification_without_storing_it_as_abstract(
    tmp_path, monkeypatch
):
    import asyncio
    from reportagent.sources import local_pdf
    from reportagent.services.collection import classify_for_storage
    from reportagent.classifiers.rule_classifier import RuleClassifier
    from reportagent.models.schemas import UserCriteria, ClassificationResult, Topic
    from types import SimpleNamespace

    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.7\n")
    monkeypatch.setattr(local_pdf, "_get_mineru_parser", lambda: None)
    monkeypatch.setattr(local_pdf, "_get_docling_parser", lambda: None)
    text = (
        "A generic study\nAuthors\n1 Introduction\n"
        + "We predict financial stock returns with portfolio risk factors. " * 5
        + "\n2 Methods\nMethodology."
    )
    source = local_pdf.LocalPDFSource(str(tmp_path))
    monkeypatch.setattr(
        source.extractor,
        "extract",
        lambda _: content(text, title="A generic study", creation_date="2024-02-03"),
    )
    sr = source._process_pdf(path, UserCriteria(topics=["量化金融"]))
    assert sr.abstract is None and sr.published_date is None
    captured = []

    async def classify(title, material):
        captured.append(material)
        return ClassificationResult(topics=[Topic.AI_ML_MODEL], method="llm")

    classified = asyncio.run(
        classify_for_storage(
            sr, RuleClassifier(), SimpleNamespace(classify=classify), []
        )
    )
    assert classified is not None
    assert captured and "We predict financial stock returns" in captured[0]
    assert "Authors" not in captured[0] and "Methodology" not in captured[0]


def test_classifier_receives_complete_section_instead_of_an_arbitrary_prefix():
    import asyncio
    from reportagent.classifiers.llm_classifier import LLMClassifier

    captured = []

    class Client:
        async def chat_json(self, messages):
            captured.extend(messages)
            return {
                "markets": [],
                "asset_classes": [],
                "frequencies": [],
                "topics": ["risk_model"],
                "custom_tags": [],
                "confidence": 0.9,
                "evidence": {
                    "topics.risk_model": {
                        "source": "title",
                        "quote": "Portfolio risk",
                        "reason": "Risk research",
                    }
                },
            }

    asyncio.run(
        LLMClassifier(Client()).classify(
            "Portfolio risk", "Research evidence. " * 200 + "TAIL_EVIDENCE"
        )
    )
    assert "TAIL_EVIDENCE" in captured[-1]["content"]


@pytest.mark.parametrize("heading", ["", "Abstract\n", "1 Introduction\n"])
@pytest.mark.parametrize(
    "prose",
    [
        "The conference paper by Chen et al. (2024) is used as a baseline.",
        "The 2024 conference paper by Chen et al. is used as a baseline.",
        "The conference proceedings of 2024 provide our benchmark data.",
        "Proceedings of the 2024 conference are used as a baseline.",
        "Proceedings from the 2024 conference provide our benchmark data.",
    ],
)
def test_conference_citation_prose_does_not_supply_publication_evidence(heading, prose):
    result = MetadataExtractor().extract(
        content("Portfolio risk estimation\n" + heading + prose)
    )
    assert result["date"] is None
    assert result["publication_date_info"] == {
        "precision": "unknown",
        "source": "unknown",
    }


@pytest.mark.parametrize(
    "imprint",
    [
        "The Thirty-Eighth AAAI Conference on Artificial Intelligence (AAAI-24)",
        "The 38th AAAI Conference on Artificial Intelligence (AAAI-24)",
        "The 2024 Conference on Empirical Methods in Natural Language Processing",
    ],
)
def test_full_conference_edition_heading_remains_supported(imprint):
    result = MetadataExtractor().extract(
        content("Paper title\nAbstract\nResearch.\n" + imprint)
    )
    assert result["publication_date_info"]["year"] == 2024
    assert result["publication_date_info"]["source"] == "conference_edition"


@pytest.mark.parametrize(
    "value,precision", [("2024", "year"), ("August 2024", "month")]
)
def test_partial_native_publication_metadata_retains_its_provenance(value, precision):
    info = MetadataExtractor().extract(content("Paper title", publication_date=value))[
        "publication_date_info"
    ]
    assert info["precision"] == precision
    assert info["source"] == "metadata:publication_date"
    assert info["evidence"] == value
