"""Shared test teardown.

Every module opens its own TestClient lifecycles against one process-wide
engine. Over a long run the pooled SQLite connections accumulate — each holding
a file handle and, under contention, a lock — until a later write blocks waiting
for one. Disposing the pool between modules returns those connections and keeps
the suite deterministic. Production runs PostgreSQL and is unaffected.
"""
import pytest


@pytest.fixture(autouse=True, scope="module")
def _release_pooled_connections():
    yield
    try:
        from app.database import engine
        engine.dispose()
    except Exception:  # noqa: BLE001
        pass
