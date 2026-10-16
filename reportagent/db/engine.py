from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from reportagent.utils.config import get_db_path

_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        db_path = get_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            echo=False,
        )
    return _engine


def get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)
    return _SessionLocal


def get_db() -> Session:
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()


def init_db():
    from reportagent.models.database import Base
    engine = get_engine()
    Base.metadata.create_all(bind=engine)

    # Add columns if missing (SQLite doesn't support ADD COLUMN IF NOT EXISTS).
    # Idempotent: inspect PRAGMA table_info first, skip columns that already exist.
    _new_columns = [
        ("knowledge_card_json", "TEXT"),
        ("selection_score", "FLOAT DEFAULT 0.0"),
        ("selection_reason", "TEXT"),
        ("is_selected", "BOOLEAN DEFAULT 0"),
        ("pushed_at", "TEXT DEFAULT NULL"),
        ("selection_direction", "VARCHAR(100)"),
        ("selection_relevance", "FLOAT"),
        ("selection_provenance_json", "TEXT"),
        ("evaluation_json", "TEXT"),
    ]
    with engine.connect() as conn:
        existing = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(reports)").fetchall()
        }
        for col_name, col_type in _new_columns:
            if col_name in existing:
                continue
            try:
                conn.exec_driver_sql(
                    f"ALTER TABLE reports ADD COLUMN {col_name} {col_type}"
                )
                conn.commit()
            except Exception:
                conn.rollback()
                # Only suppress a concurrent successful addition, not a failed migration.
                actual = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(reports)")}
                if col_name not in actual:
                    raise
