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


def test_optimal_value_asterisks_do_not_turn_intervening_prose_italic():
    source = "**方法与发现**：期货价格m*由零价值条件确定，作者给出显式收缩界；误差至多为10−3(X0−m*)。"
    assert (
        card_markdown(source)
        == "**方法与发现**：期货价格m∗由零价值条件确定，作者给出显式收缩界；误差至多为10−3(X0−m∗)。"
    )
    assert "m*" in source  # The saved assessment is not rewritten.


def test_math_star_conversion_preserves_intentional_formatting_code_and_urls():
    source = (
        "*强调* 和 *m* 与 **粗体**，*use x* 以及中文*强调 x*，数学x*、K_t*、x_1*。\n"
        "`m*` ``x* 与 `code` `` [原文](https://example.org/m*) "
        "https://example.org/x*\n```text\nm*\n```"
    )
    expected = source.replace("数学x*、K_t*、x_1*", "数学x∗、K_t∗、x_1∗")
    assert card_markdown(source) == expected


def test_math_stars_are_normalized_in_each_card_section():
    sections = card_sections("## 方法\n价格m*使价值为零\n\n## 误差\n界为X0−m*。")
    assert sections[0]["content"] == "**方法**\n价格m∗使价值为零"
    assert sections[2]["content"] == "**误差**\n界为X0−m∗。"
