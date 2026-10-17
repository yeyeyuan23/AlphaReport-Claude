from reportagent.services.feishu_markdown import card_markdown, card_sections


def test_blockquotes_are_readable_without_exposing_quote_markers_in_cards():
    text = "> ⚠ 采集时有两篇 PDF 未取得全文\n\n> ## 说明\n> 保留原文\n```text\n> literal quote marker\n```\na > b"
    assert (
        card_markdown(text)
        == "⚠ 采集时有两篇 PDF 未取得全文\n\n**说明**\n保留原文\n```text\n> literal quote marker\n```\na > b"
    )


def test_all_heading_levels_use_card_supported_bold_labels():
    source = "\n".join("#" * n + " 小节" + str(n) for n in range(1, 7))
    assert card_markdown(source) == "\n".join(
        "**小节" + str(n) + "**" for n in range(1, 7)
    )
    assert card_markdown("## 小节 ##\n### **已有粗体**") == "**小节**\n**已有粗体**"


def test_card_title_is_omitted_once_but_later_top_level_sections_remain():
    assert (
        card_markdown("\n# 周报\n正文\n# 附录", omit_leading_title=True)
        == "\n正文\n**附录**"
    )
    assert (
        card_markdown("说明\n# 后续标题", omit_leading_title=True)
        == "说明\n**后续标题**"
    )


def test_hashes_in_prose_links_inline_code_and_fenced_examples_are_not_headings():
    source = (
        "C# 模型\n#hashtag\n[原文](https://example.org/#abstract)\n"
        "`## inline`\n    ## indented code\n```python\n## code comment\n```\n"
        "~~~text\n# literal\n~~~\n## 真正的小节"
    )
    assert card_markdown(source) == source.replace("## 真正的小节", "**真正的小节**")


def test_native_section_breaks_preserve_long_papers_and_fenced_headings():
    body = "正文与原文引证\n" * 40
    text = (
        "# 日报\n\n## 待核验\n"
        + body
        + "```text\n## 代码中的标题\n```\n\n## 来源覆盖\n正常"
    )
    elements = card_sections(text, omit_leading_title=True)
    assert [e["tag"] for e in elements] == ["markdown", "hr", "markdown"]
    assert body in elements[0]["content"]
    assert "```text\n## 代码中的标题\n```" in elements[0]["content"]
    assert elements[1] == {"tag": "hr"}
    assert elements[2]["content"] == "**来源覆盖**\n正常"
