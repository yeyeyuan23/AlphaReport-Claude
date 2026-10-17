"""Collection notices distinguish historical material gaps from source outages."""

import pytest


def collection(**changes):
    value = {
        "status": "partial",
        "completed_at": "2026-10-17T00:06:23+00:00",
        "storage": {"added": 15, "updated": 0},
        "source_statuses": [
            {
                "source": "arxiv",
                "status": "partial",
                "valid": 15,
                "full_text": 13,
                "errors": [
                    "PDF download/parse: download exceeds size limit",
                    "PDF download/parse: TimeoutError",
                ],
            }
        ],
    }
    value.update(changes)
    return value


def test_pdf_only_failures_show_counts_and_do_not_claim_a_source_outage():
    from reportagent.services.scheduled_collection import collection_notice

    text = collection_notice(collection())
    assert "2026-10-17 08:06" in text
    assert "新增 15 篇" in text
    assert "13 条取得全文" in text and "2 条未取得全文" in text
    assert "采集时" in text and "大小上限" in text
    assert "下载或解析超时" in text
    assert "覆盖可能不完整" not in text
    assert "45" not in text  # An old timeout record does not reveal its deadline.


def test_failed_search_discloses_coverage_without_inventing_counts():
    from reportagent.services.scheduled_collection import collection_notice

    text = collection_notice(
        collection(
            storage={},
            source_statuses=[
                {
                    "source": "arxiv",
                    "status": "error",
                    "valid": 0,
                    "errors": ["search: ReadTimeout"],
                }
            ],
        )
    )
    assert "arXiv" in text and "搜索请求超时" in text
    assert "来源覆盖可能不完整" in text
    assert "新增 0" not in text and "未取得全文" not in text


@pytest.mark.parametrize(
    "status,label", [("failed", "失败"), ("cancelled", "取消"), ("partial", "部分完成")]
)
def test_run_level_failures_remain_visible_without_source_details(status, label):
    from reportagent.services.scheduled_collection import collection_notice

    text = collection_notice(
        collection(status=status, source_statuses=[], storage={}, error="TimeoutError")
    )
    assert label in text and "超时" in text


def test_successful_collection_needs_no_warning():
    from reportagent.services.scheduled_collection import collection_notice

    assert collection_notice(collection(status="ok")) is None
