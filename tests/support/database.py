"""Function-scoped SQLite fixture for same-thread persistence regressions."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reportagent.models.database import Base


@pytest.fixture
def memory_session_factory():
    """Keep non-expiring sessions; HTTP and concurrent/file DB fixtures stay local."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()
