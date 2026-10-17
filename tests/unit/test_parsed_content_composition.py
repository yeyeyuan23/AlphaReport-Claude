"""Structured metadata supplements full text without repeating exported content."""

import copy
from types import SimpleNamespace

import pytest

from reportagent.processors.docling_parser import DoclingParser
from reportagent.processors.mineru_parser import MinerUParser, ParsedContent
from reportagent.services.pdf_extraction import parse_pdf

TABLE = "| factor | score |\n| --- | --- |\n| momentum | 0.71 |"
CAPTION = "Table 1: Out-of-sample performance"
FORMULA = r"\sum_{i=1}^{n} w_i r_i"


def docling_document(markdown, *, export_fails=False):
    title = SimpleNamespace(
        label="title",
        text="Paper title",
        self_ref="title",
        prov=[SimpleNamespace(page_no=1)],
    )
    table = SimpleNamespace(
        label="table",
        prov=[SimpleNamespace(page_no=2)],
        export_to_markdown=lambda **kwargs: TABLE,
        caption_text=lambda doc: CAPTION,
    )
    formula = SimpleNamespace(
        label="formula",
        text=FORMULA,
        orig=FORMULA,
        self_ref="formula",
        prov=[SimpleNamespace(page_no=2)],
    )

    def export():
        if export_fails:
            raise ValueError("Offline export failure")
        return markdown

    return SimpleNamespace(
        texts=[title, formula],
        iterate_items=lambda: iter([(title, 0), (table, 0), (formula, 0)]),
        export_to_markdown=export,
    )


def test_docling_complete_markdown_keeps_metadata_without_duplication():
    markdown = f"# Paper\n\n{CAPTION}\n\n{TABLE}\n\n$$\n{FORMULA}\n$$\n"
    parsed = DoclingParser()._build_result(docling_document(markdown), "paper.pdf")
    snapshot = copy.deepcopy(parsed.__dict__)

    assert parsed.full_text_with_tables == markdown
    assert parsed.full_text_with_tables == markdown
    assert parsed.__dict__ == snapshot
    assert parsed.tables[0]["table_body"] == TABLE
    assert parsed.equations[0]["latex"] == FORMULA
    assert parsed.first_page_text == "Paper title"


def test_docling_failed_markdown_export_still_appends_missing_table_and_caption():
    parsed = DoclingParser()._build_result(
        docling_document("", export_fails=True), "paper.pdf"
    )

    output = parsed.full_text_with_tables
    assert output.count(TABLE) == output.count(CAPTION) == output.count(FORMULA) == 1
    assert parsed.first_page_text == "Paper title"


def test_mineru_separate_metadata_still_appends_in_full():
    parsed = MinerUParser()._build_result(
        [
            {"type": "text", "text": "Independent evidence.", "page_idx": 0},
            {"type": "table", "table_body": TABLE, "table_caption": [CAPTION]},
            {"type": "equation", "latex": FORMULA},
        ],
        "paper.pdf",
    )

    assert parsed.full_text_with_tables == (
        f"Independent evidence.\n\n[Table: {CAPTION}]\n{TABLE}\n\n[Equation] {FORMULA}"
    )
    assert parsed.first_page_text == "Independent evidence."


@pytest.mark.parametrize("embedded", [TABLE, CAPTION, f"{CAPTION}\n{TABLE}"])
def test_caption_and_body_are_checked_independently(embedded):
    parsed = ParsedContent(
        text=f"Evidence\n{embedded}",
        tables=[{"table_body": TABLE, "table_caption": [CAPTION]}],
    )

    output = parsed.full_text_with_tables
    assert output.count(TABLE) == output.count(CAPTION) == 1


def test_missing_caption_parts_are_preserved_without_repeating_present_parts():
    parsed = ParsedContent(
        text=f"Evidence\n{CAPTION}\n{TABLE}",
        tables=[{"table_body": TABLE, "table_caption": [CAPTION, "Panel B: Risk"]}],
    )

    output = parsed.full_text_with_tables
    assert output.count(TABLE) == output.count(CAPTION) == 1
    assert output.count("Panel B: Risk") == 1


def test_table_outer_whitespace_and_line_endings_do_not_repeat_data():
    embedded = "\r\n".join(f"  {line}  " for line in TABLE.splitlines())
    parsed = ParsedContent(
        text=f"Evidence\n{embedded}", tables=[{"table_body": f"\n{TABLE}\n"}]
    )

    assert parsed.full_text_with_tables == parsed.text


def test_changed_table_value_is_not_discarded():
    changed = TABLE.replace("0.71", "0.72")
    parsed = ParsedContent(text=TABLE, tables=[{"table_body": changed}])

    assert TABLE in parsed.full_text_with_tables
    assert changed in parsed.full_text_with_tables


@pytest.mark.parametrize(
    "template", ["$$\n{}\n$$", "$${}$$", r"\[{}\]", r"\({}\)", "${}$", "{}"]
)
@pytest.mark.parametrize("wrapped_metadata", [False, True])
def test_complete_equation_is_not_repeated(template, wrapped_metadata):
    latex = f"$$\n{FORMULA}\n$$" if wrapped_metadata else FORMULA
    text = "Evidence\n" + template.format(FORMULA) + "\nDiscussion"
    parsed = ParsedContent(text=text, equations=[{"latex": latex}])

    assert parsed.full_text_with_tables == text


@pytest.mark.parametrize(
    ("latex", "text"),
    [
        ("1", "We use 10 factors and 1 baseline."),
        ("x", "The text explains factor extraction."),
        ("r_i", "The indicator_i column contains returns."),
        ("x=1", "The string x=10 is the control name."),
        ("1", "$10$"),
        ("x", "$$x^2$$"),
        ("x", "$$\nx\n+ y\n$$"),
        ("1", "\\[\n1\n+ 2\n\\]"),
        ("x", r"The cost is \$x\$ per observation."),
        ("x=1", "$$x=10$$"),
        (r"\alpha", r"\[\alpha^2\]"),
        ("x + z", "$$x + y$$"),
        (r"\text{ab c}", r"$$\text{abc}$$"),
    ],
)
def test_incidental_substrings_and_different_equations_still_append(latex, text):
    parsed = ParsedContent(text=text, equations=[{"latex": latex}])

    assert parsed.full_text_with_tables == f"{text}\n\n[Equation] {latex}"


def test_repeated_metadata_adds_each_missing_structure_only_once_without_mutation():
    table = {"table_body": TABLE, "table_caption": [CAPTION]}
    equation = {"latex": FORMULA}
    parsed = ParsedContent(
        text="Evidence", tables=[table, table], equations=[equation, equation]
    )
    snapshot = copy.deepcopy(parsed.__dict__)

    output = parsed.full_text_with_tables
    assert output.count(TABLE) == output.count(CAPTION) == output.count(FORMULA) == 1
    assert parsed.full_text_with_tables == output
    assert parsed.__dict__ == snapshot


def test_shared_merge_retains_primary_structures_missing_from_selected_markdown():
    unique_table = TABLE.replace("momentum", "value")
    unique_equation = r"\sigma^2 = E[(r - \mu)^2]"
    primary = ParsedContent(
        text="Primary evidence.",
        tables=[{"table_body": TABLE}, {"table_body": unique_table}],
        equations=[{"latex": FORMULA}, {"latex": unique_equation}],
        first_page_text="Primary title",
    )
    markdown = "Detailed research evidence. " * 100 + f"\n{TABLE}\n$${FORMULA}$$"
    secondary = ParsedContent(
        text=markdown,
        tables=[{"table_body": TABLE}],
        equations=[{"latex": FORMULA}],
        first_page_text="Secondary title",
    )

    parsed = parse_pdf(
        "paper.pdf",
        mineru_parser=SimpleNamespace(parse=lambda _: primary),
        docling_parser=SimpleNamespace(parse=lambda _: secondary),
    )

    output = parsed.full_text_with_tables
    assert parsed.text == markdown
    assert output.count(TABLE) == output.count(FORMULA) == 1
    assert output.count(unique_table) == output.count(unique_equation) == 1
    assert len(parsed.tables) == len(parsed.equations) == 2
    assert parsed.first_page_text == "Secondary title"


@pytest.mark.parametrize("latex", ["1", "x", "r_i", r"\alpha", "-1"])
def test_bare_page_numbers_or_variable_headings_do_not_suppress_equation(latex):
    text = f"Results\n{latex}\nMore prose"
    parsed = ParsedContent(text=text, equations=[{"latex": latex}])

    assert parsed.full_text_with_tables == f"{text}\n\n[Equation] {latex}"


@pytest.mark.parametrize("latex", ["1", "x", "r_i", r"\alpha"])
def test_explicit_equation_context_proves_even_atom_is_present(latex):
    text = f"Results\n[Equation] {latex}\nMore prose"
    parsed = ParsedContent(text=text, equations=[{"latex": latex}])

    assert parsed.full_text_with_tables == text


@pytest.mark.parametrize(
    "content",
    [
        ParsedContent(text="Evidence", tables=[{"table_body": None}]),
        ParsedContent(text="Evidence", equations=[{"latex": None}]),
        MinerUParser()._build_result(
            [
                {"type": "text", "text": "Evidence"},
                {"type": "table", "table_body": None},
                {"type": "equation", "latex": None, "text": ""},
            ],
            "paper.pdf",
        ),
    ],
)
def test_absent_null_structures_are_still_skipped(content):
    assert content.full_text_with_tables == "Evidence"
