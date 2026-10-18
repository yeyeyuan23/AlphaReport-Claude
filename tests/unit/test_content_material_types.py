"""Material type comes from document evidence, independently of its channel."""

import json

import pytest
from sqlalchemy.orm import sessionmaker

from reportagent.db.repository import ReportRepository
from reportagent.models.database import Report
from reportagent.models.schemas import (
    ClassifiedReport,
    ClassificationResult,
    SearchResult,
    SourceType,
)


def incoming(source=SourceType.BIGQUANT, **changes):
    values = dict(
        title="研究材料",
        source=source,
        full_text="证券研究报告\n发布日期：2024-02-03\n摘要\n组合优化方法。",
        abstract_only=False,
    )
    values.update(changes)
    return ClassifiedReport(
        search_result=SearchResult(**values), classification=ClassificationResult()
    )


@pytest.mark.parametrize(
    "source",
    [SourceType.BIGQUANT, SourceType.ARXIV, SourceType.EASTMONEY, SourceType.LOCAL_PDF],
)
def test_document_header_controls_type_across_import_channels(
    memory_session_factory, source
):
    with memory_session_factory() as session:
        _, rid = ReportRepository(session).upsert_report(incoming(source))
        row = session.get(Report, rid)
        assert row.source == source.value
        assert row.material_type == "broker"
        assert json.loads(row.material_type_info_json)["evidence"] == "证券研究报告"


def test_reposted_broker_self_attribution_counts_as_document_evidence(
    memory_session_factory,
):
    with memory_session_factory() as session:
        _, rid = ReportRepository(session).upsert_report(
            incoming(
                full_text="本文来自方正证券研究所于2023年2月15日发布的报告《多因子选股研究》，请阅读报告原文。\n摘要\n方法。"
            )
        )
        row = session.get(Report, rid)
        assert row.material_type == "broker"
        assert (
            "本文来自方正证券研究所"
            in json.loads(row.material_type_info_json)["evidence"]
        )


@pytest.mark.parametrize(
    "source", [SourceType.BIGQUANT, SourceType.ARXIV, SourceType.EASTMONEY]
)
def test_channel_alone_does_not_establish_material_type(memory_session_factory, source):
    with memory_session_factory() as session:
        _, rid = ReportRepository(session).upsert_report(
            incoming(source, full_text="没有文档类型证据的讨论。")
        )
        assert session.get(Report, rid).material_type == "unknown"


def test_reimport_replaces_an_old_source_based_guess(memory_session_factory):
    with memory_session_factory() as session:
        report = incoming()
        repo = ReportRepository(session)
        _, rid = repo.upsert_report(report)
        row = session.get(Report, rid)
        row.material_type = "community"
        row.material_type_info_json = json.dumps(
            {"source": "typed_feed", "evidence": "bigquant"}
        )
        session.commit()
        repo.upsert_report(report)
        assert session.get(Report, rid).material_type == "broker"


def test_initialization_repairs_old_guesses_and_is_idempotent(
    memory_session_factory, monkeypatch
):
    from reportagent.db import engine as db

    with memory_session_factory() as session:
        engine = session.get_bind()
        for rid, title, source, kind, info, text, aid in [
            (
                1,
                "华安证券-风险预算研究报告",
                "bigquant",
                "community",
                "typed_feed",
                None,
                None,
            ),
            (
                2,
                "问题A",
                "bigquant",
                "community",
                "typed_feed",
                "没有材料类型证据",
                None,
            ),
            (
                3,
                "转载报告",
                "bigquant",
                "community",
                "typed_feed",
                "证券研究报告\n摘要\n内容",
                None,
            ),
            (
                4,
                "有明确来源的原创",
                "bigquant",
                "community",
                "document_header",
                "BigQuant 社区研究",
                None,
            ),
            (
                5,
                "本地论文",
                "local_pdf",
                "unknown",
                "unknown",
                "Published as a conference paper at ICLR 2024\nAbstract\nStudy",
                None,
            ),
            (6, "无证据的旧数据", "arxiv", "academic", "typed_feed", None, None),
            (
                7,
                "明确论文编号",
                "arxiv",
                "academic",
                "typed_feed",
                None,
                "2402.00001v1",
            ),
        ]:
            session.add(
                Report(
                    id=rid,
                    title=title,
                    source=source,
                    material_type=kind,
                    material_type_info_json=json.dumps(
                        {
                            "source": info,
                            "evidence": source if info == "typed_feed" else text,
                        }
                    ),
                    full_text=text,
                    arxiv_id=aid,
                    content_hash=str(rid),
                )
            )
        session.commit()
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    db.init_db()
    db.init_db()
    with sessionmaker(bind=engine)() as session:
        assert [
            (r.id, r.material_type) for r in session.query(Report).order_by(Report.id)
        ] == [
            (1, "broker"),
            (2, "unknown"),
            (3, "broker"),
            (4, "community"),
            (5, "academic"),
            (6, "unknown"),
            (7, "academic"),
        ]
        assert all(
            json.loads(r.material_type_info_json).get("source") != "typed_feed"
            for r in session.query(Report)
        )


def test_source_statistics_allow_mixed_document_types(memory_session_factory):
    with memory_session_factory() as session:
        for rid, kind in enumerate(("broker", "broker", "community", "unknown"), 1):
            session.add(
                Report(
                    id=rid,
                    title=str(rid),
                    source="bigquant",
                    material_type=kind,
                    content_hash=str(rid),
                )
            )
        session.commit()
        stats = ReportRepository(session).get_stats()
        assert stats["by_source"] == [
            {
                "source": "bigquant",
                "count": 4,
                "material_types": {"broker": 2, "community": 1, "unknown": 1},
            }
        ]


def test_broker_name_in_company_news_is_not_report_type_evidence(
    memory_session_factory,
):
    with memory_session_factory() as session:
        _, rid = ReportRepository(session).upsert_report(
            incoming(
                title="华安证券-股东大会公告", full_text="本次大会通过了公司议案。"
            )
        )
        assert session.get(Report, rid).material_type == "unknown"


@pytest.mark.parametrize(
    "header",
    [
        "国联期货研究所\n分析师：甲\n投资咨询号：Z0001\n摘要\n研究内容。",
        "策略说明：\n金融街证券研究所基于策略报告《行业轮动研究》，构建ETF组合。\n策略更新：\n本期研究内容。",
    ],
)
def test_original_publisher_and_report_role_identify_broker_research(
    memory_session_factory, header
):
    with memory_session_factory() as session:
        _, rid = ReportRepository(session).upsert_report(incoming(full_text=header))
        row = session.get(Report, rid)
        assert row.material_type == "broker"
        assert json.loads(row.material_type_info_json)["source"] == "document_header"
