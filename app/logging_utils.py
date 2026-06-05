"""Helpers for per-refresh logging context."""

from contextlib import contextmanager
from contextvars import ContextVar, Token
import logging
from uuid import uuid4

_refresh_id_var: ContextVar[str | None] = ContextVar("refresh_id", default=None)
_configured = False
_previous_factory = None


def _record_factory(*args, **kwargs):
    if _previous_factory is None:  # pragma: no cover - guarded by configure_refresh_logging
        record = logging.LogRecord(*args, **kwargs)
    else:
        record = _previous_factory(*args, **kwargs)
    refresh_id = _refresh_id_var.get()
    record.refresh_id = refresh_id or "-"
    record.refresh_prefix = f"[{refresh_id}] " if refresh_id else ""
    return record


def configure_refresh_logging() -> None:
    global _configured, _previous_factory
    if _configured:
        return
    _previous_factory = logging.getLogRecordFactory()
    logging.setLogRecordFactory(_record_factory)
    _configured = True


def new_refresh_id() -> str:
    return f"refresh-{uuid4().hex[:4]}"


def get_refresh_id() -> str | None:
    return _refresh_id_var.get()


def set_refresh_id(refresh_id: str) -> Token[str | None]:
    return _refresh_id_var.set(refresh_id)


def reset_refresh_id(token: Token[str | None]) -> None:
    _refresh_id_var.reset(token)


@contextmanager
def refresh_logging_context(refresh_id: str):
    token = set_refresh_id(refresh_id)
    try:
        yield refresh_id
    finally:
        reset_refresh_id(token)
