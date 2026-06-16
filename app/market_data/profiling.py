"""Diagnostic helpers for profiling the market-data refresh path.

All instrumentation here is gated behind
:func:`app.config.is_refresh_profiling_enabled`. When the flag is off, the
helpers are no-ops so production hot paths pay nothing for them.

Designed to answer: how many SQL round-trips does one refresh actually make
against Postgres/Supabase, broken down by phase, and how long did each phase
spend in SQL vs in Python.
"""

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from sqlalchemy import event

from app.config import is_refresh_profiling_enabled

logger = logging.getLogger(__name__)


@dataclass
class _ProfileScope:
    query_count: int = 0
    query_time: float = 0.0
    seed_invocations: int = 0
    block_wall: dict[str, list[float]] = field(default_factory=dict)
    block_queries: dict[str, int] = field(default_factory=dict)
    block_query_time: dict[str, float] = field(default_factory=dict)


_state = threading.local()


def _current_scope() -> _ProfileScope | None:
    return getattr(_state, "scope", None)


def is_profiling_active() -> bool:
    """Return True when a profiling scope is currently active on this thread."""
    return _current_scope() is not None


def record_seed_invocation() -> None:
    """Increment the per-refresh seed-defaults invocation counter, if active."""
    scope = _current_scope()
    if scope is not None:
        scope.seed_invocations += 1


@contextmanager
def time_block(label: str):
    """Record wall time + sql queries + sql time spent inside a labelled block.

    No-op when profiling is not active. Multiple blocks with the same label
    accumulate.
    """
    scope = _current_scope()
    if scope is None:
        yield
        return
    started = time.monotonic()
    queries_before = scope.query_count
    sql_time_before = scope.query_time
    try:
        yield
    finally:
        scope.block_wall.setdefault(label, []).append(time.monotonic() - started)
        scope.block_queries[label] = scope.block_queries.get(label, 0) + (
            scope.query_count - queries_before
        )
        scope.block_query_time[label] = scope.block_query_time.get(label, 0.0) + (
            scope.query_time - sql_time_before
        )


@contextmanager
def refresh_profiling_scope(engine, *, tag: str):
    """Activate per-thread query counting + block timing for a refresh.

    Attaches SQLAlchemy ``before_cursor_execute`` / ``after_cursor_execute``
    listeners for the duration of the ``with`` block. On exit, logs a single
    summary line with total queries, total SQL time, wall time, and per-block
    breakdowns.

    Yields the active ``_ProfileScope`` (or ``None`` when the feature flag is
    off) so callers can read counts before the scope closes if useful.
    """
    if not is_refresh_profiling_enabled():
        yield None
        return

    scope = _ProfileScope()
    prev = getattr(_state, "scope", None)
    _state.scope = scope

    def _before_cursor(conn, cursor, statement, parameters, context, executemany):
        context._refresh_profile_started_at = time.monotonic()

    def _after_cursor(conn, cursor, statement, parameters, context, executemany):
        scope.query_count += 1
        started_at = getattr(context, "_refresh_profile_started_at", None)
        if started_at is not None:
            scope.query_time += time.monotonic() - started_at

    event.listen(engine, "before_cursor_execute", _before_cursor)
    event.listen(engine, "after_cursor_execute", _after_cursor)
    wall_started = time.monotonic()
    try:
        yield scope
    finally:
        event.remove(engine, "before_cursor_execute", _before_cursor)
        event.remove(engine, "after_cursor_execute", _after_cursor)
        _state.scope = prev
        wall = time.monotonic() - wall_started
        block_summary = " ".join(
            (
                f"{label}=("
                f"wall={sum(scope.block_wall[label]):.3f}s/"
                f"calls={len(scope.block_wall[label])}/"
                f"q={scope.block_queries.get(label, 0)}/"
                f"sql={scope.block_query_time.get(label, 0.0):.3f}s)"
            )
            for label in sorted(scope.block_wall)
        )
        logger.info(
            "[profile %s] wall=%.3fs queries=%d sql_time=%.3fs "
            "ensure_defaults_calls=%d %s",
            tag,
            wall,
            scope.query_count,
            scope.query_time,
            scope.seed_invocations,
            block_summary,
        )
