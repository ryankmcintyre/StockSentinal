"""Tests for the database module engine creation logic."""

from sqlalchemy.pool import NullPool, QueuePool

from app.database import _create_engine


class TestCreateEngine:
    def test_sqlite_uses_check_same_thread_false(self):
        engine = _create_engine("sqlite:///./test.db")
        # SQLite engines should not use NullPool
        assert not isinstance(engine.pool, NullPool)
        engine.dispose()

    def test_sqlite_memory(self):
        engine = _create_engine("sqlite:///:memory:")
        assert engine.url.get_backend_name() == "sqlite"
        engine.dispose()

    def test_postgres_uses_queue_pool(self):
        # We can't actually connect to Postgres in unit tests, but we can
        # verify the engine is configured with a real client-side pool
        # (not NullPool) so connection handshakes are amortized across
        # requests instead of paid on every request.
        engine = _create_engine(
            "postgresql+psycopg2://user:pass@localhost:5432/testdb"
        )
        assert not isinstance(engine.pool, NullPool)
        assert isinstance(engine.pool, QueuePool)
        assert engine.pool.size() == 5
        engine.dispose()

    def test_default_reads_from_config(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        engine = _create_engine()
        assert engine.url.get_backend_name() == "sqlite"
        engine.dispose()
