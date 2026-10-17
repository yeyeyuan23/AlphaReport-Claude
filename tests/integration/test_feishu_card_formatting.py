"""Inspect outbound card JSON without sending to Feishu."""

import json
import re
from unittest.mock import MagicMock, patch

import pytest

from reportagent.services import daily_push, weekly_report


@pytest.mark.parametrize("service", [daily_push, weekly_report])
def test_report_card_formats_headings_without_changing_evidence_or_links(service):
    source = (
        "# 量化研究简报\n\n本期检查 2 篇。\n\n## 重点推荐\n\n"
        "### 1. 条件性结论\n只有温度充分小时才保持凸性。\n"
        "[原文](https://example.org/paper#abstract)\n\n"
        + "\n".join(f"补充证据 {i}" for i in range(10))
        + "\n\n## 来源覆盖\n来源检查正常。"
    )
    original = source
    client = MagicMock()
    client.im.v1.message.create.return_value.success.return_value = True
    builder = MagicMock()
    builder.app_id.return_value = builder
    builder.app_secret.return_value = builder
    builder.build.return_value = client
    import lark_oapi

    with patch.object(lark_oapi.Client, "builder", return_value=builder):
        if service is weekly_report:
            ok = service.send_feishu_card(
                "test-chat", source, [], "2026-10-05", "2026-10-11"
            )
        else:
            ok = service.send_feishu_card("test-chat", source, [], "2026-10-17")
    assert ok
    request = client.im.v1.message.create.call_args.args[0]
    card = json.loads(request.request_body.content)
    markdown = "\n".join(
        e["content"] for e in card["elements"] if e["tag"] == "markdown"
    )
    assert not re.search(r"^\s*#{1,6}\s+", markdown, re.M)
    assert "**重点推荐**" in markdown
    assert "**1. 条件性结论**" in markdown
    assert "**来源覆盖**" in markdown
    assert "只有温度充分小时才保持凸性。" in markdown
    assert "[原文](https://example.org/paper#abstract)" in markdown
    assert all(f"补充证据 {i}" in markdown for i in range(10))
    assert source == original  # Saved Markdown remains suitable for normal readers.
    if service is daily_push:
        assert "共收录" not in markdown


@pytest.mark.parametrize("service", [daily_push, weekly_report])
def test_coverage_has_a_section_break_after_pending_paper_links(service):
    source = (
        "# 日报\n\n概览\n\n## 待核验\n"
        "- **Paper** · 待核验\n  需要核对原文。\n"
        "  [原文](https://example.org/paper) · [网页详情](https://example.org/library/1)\n\n"
        "## 来源覆盖\n\n- arxiv：正常，有效 1 篇"
    )
    client = MagicMock()
    client.im.v1.message.create.return_value.success.return_value = True
    builder = MagicMock()
    builder.app_id.return_value = builder
    builder.app_secret.return_value = builder
    builder.build.return_value = client
    import lark_oapi

    with patch.object(lark_oapi.Client, "builder", return_value=builder):
        if service is weekly_report:
            assert service.send_feishu_card(
                "test-chat", source, [], "2026-10-05", "2026-10-11"
            )
        else:
            assert service.send_feishu_card("test-chat", source, [], "2026-10-17")
    card = json.loads(
        client.im.v1.message.create.call_args.args[0].request_body.content
    )
    blocks = card["elements"]
    coverage = next(
        i
        for i, e in enumerate(blocks)
        if e.get("tag") == "markdown"
        and e.get("content", "").startswith("**来源覆盖**")
    )
    assert blocks[coverage - 1]["tag"] == "hr"
    assert "[网页详情]" in blocks[coverage - 2]["content"]
    assert not any(
        "**来源覆盖**" in e.get("content", "") and "[网页详情]" in e.get("content", "")
        for e in blocks
    )
