"""Unit tests for the refresh-profiling instrumentation helpers."""

import logging

import pytest
from sqlalchemy import create_engine, text

from app.market_data import profiling


@pytest.fixture
def in_memory_engine():
    engine = create_engine("sqlite:///:memory:")
    try:
        yield engine
    finally:
        engine.dispose()


def test_scope_is_noop_when_flag_disabled(monkeypatch, in_memory_engine):
    monkeypatch.setattr(profiling, "is_refresh_profiling_enabled", lambda: False)
    with profiling.refresh_profiling_scope(in_memory_engine, tag="test") as scope:
        assert scope is None
        # time_block and record_seed_invocation must still be safe to call.
        with profiling.time_block("noop"):
            pass
        profiling.record_seed_invocation()
        assert not profiling.is_profiling_active()


def test_scope_counts_queries_seeds_and_blocks(monkeypatch, in_memory_engine, caplog):
    monkeypatch.setattr(profiling, "is_refresh_profiling_enabled", lambda: True)
    caplog.set_level(logging.INFO, logger="app.market_data.profiling")

    with profiling.refresh_profiling_scope(in_memory_engine, tag="unit") as scope:
        assert scope is not None
        assert profiling.is_profiling_active()
        with profiling.time_block("setup"):
            with in_memory_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                conn.execute(text("SELECT 2"))
        profiling.record_seed_invocation()
        profiling.record_seed_invocation()
        with profiling.time_block("teardown"):
            with in_memory_engine.connect() as conn:
                conn.execute(text("SELECT 3"))

    assert scope.query_count == 3
    assert scope.seed_invocations == 2
    assert scope.block_queries == {"setup": 2, "teardown": 1}
    assert set(scope.block_wall.keys()) == {"setup", "teardown"}
    # Summary log line is emitted on scope exit.
    assert any("[profile unit]" in record.message for record in caplog.records)


def test_listeners_removed_after_scope_exits(monkeypatch, in_memory_engine):
    """After the scope exits, further queries must not be counted by a new scope's predecessor listener."""
    monkeypatch.setattr(profiling, "is_refresh_profiling_enabled", lambda: True)

    with profiling.refresh_profiling_scope(in_memory_engine, tag="first") as first:
        with in_memory_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        assert first.query_count == 1

    # Running a query outside any scope must not raise and must not leak into
    # the next scope.
    with in_memory_engine.connect() as conn:
        conn.execute(text("SELECT 99"))

    with profiling.refresh_profiling_scope(in_memory_engine, tag="second") as second:
        with in_memory_engine.connect() as conn:
            conn.execute(text("SELECT 2"))
        assert second.query_count == 1
