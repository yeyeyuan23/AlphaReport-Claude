"""Fresh full-schema SQLite databases for same-thread persistence regressions."""

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.models.database import Base


@contextmanager
def memory_database(*, expire_on_commit=True):
    """Dispose even on setup/test failure; HTTP and file/thread DBs stay local."""
    engine = create_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        yield sessionmaker(bind=engine, expire_on_commit=expire_on_commit)
    finally:
        engine.dispose()


@pytest.fixture
def memory_session_factory():
    """Preserve the existing non-expiring, function-scoped fixture contract."""
    with memory_database(expire_on_commit=False) as factory:
        yield factory


@pytest.fixture
def expiring_memory_session_factory():
    """Keep SQLAlchemy's default expiration for service-facing factories."""
    with memory_database() as factory:
        yield factory
