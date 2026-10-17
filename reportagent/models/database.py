from __future__ import annotations

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    DateTime,
    Boolean,
    Float,
    ForeignKey,
    Table,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


report_tags = Table(
    "report_tags",
    Base.metadata,
    Column(
        "report_id",
        Integer,
        ForeignKey("reports.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    ),
)


class Report(Base):
    __tablename__ = "reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(500), nullable=False, index=True)
    authors = Column(Text)
    abstract = Column(Text)
    full_text = Column(Text)
    has_full_text = Column(Boolean, default=False)
    source = Column(String(50), nullable=False)
    source_url = Column(String(1000))
    doi = Column(String(200), index=True)
    arxiv_id = Column(String(50), index=True)
    published_date = Column(DateTime)
    publication_date_info_json = Column(Text)
    pdf_path = Column(String(500))
    content_hash = Column(String(64), unique=True, nullable=False, index=True)

    summary = Column(Text)
    tables_json = Column(Text)
    equations_json = Column(Text)

    markets = Column(String(200))
    asset_classes = Column(String(200))
    frequencies = Column(String(200))
    topics = Column(String(200))
    classification_confidence = Column(Float, default=0.0)
    classification_method = Column(String(20))
    classification_provenance_json = Column(Text)

    analysis_json = Column(Text)
    knowledge_card_json = Column(Text)

    # LLM selection scoring for daily push curation
    selection_score = Column(Float, default=0.0)
    selection_reason = Column(Text)
    selection_direction = Column(String(100))
    selection_relevance = Column(Float)
    selection_provenance_json = Column(Text)
    evaluation_json = Column(Text)
    is_selected = Column(Boolean, default=False)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    tags = relationship("Tag", secondary=report_tags, back_populates="reports")


class LocalPDFImport(Base):
    """Successful local file imports; failed work remains eligible for retry."""

    __tablename__ = "local_pdf_imports"

    file_path = Column(String(1000), primary_key=True)
    file_sha256 = Column(String(64), nullable=False)
    report_id = Column(Integer, ForeignKey("reports.id", ondelete="SET NULL"))
    report_content_hash = Column(String(64))
    status = Column(String(20), nullable=False)


class Tag(Base):
    __tablename__ = "tags"

    id = Column(Integer, primary_key=True, autoincrement=True)
    dimension = Column(String(50), nullable=False)
    value = Column(String(100), nullable=False)

    reports = relationship("Report", secondary=report_tags, back_populates="tags")

    __table_args__ = (UniqueConstraint("dimension", "value"),)


class SearchHistory(Base):
    __tablename__ = "search_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(36), unique=True, nullable=False, index=True)
    criteria_json = Column(Text, nullable=False)
    status = Column(String(20), default="pending")
    results_count = Column(Integer, default=0)
    newly_added = Column(Integer, default=0)
    errors_json = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    completed_at = Column(DateTime)


class ReportRun(Base):
    __tablename__ = "report_runs"

    run_id = Column(String(36), primary_key=True)
    status = Column(String(30), nullable=False)
    result_json = Column(Text, nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class UserCriteriaPreset(Base):
    __tablename__ = "criteria_presets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, unique=True)
    criteria_json = Column(Text, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    created_at = Column(DateTime, server_default=func.now())
