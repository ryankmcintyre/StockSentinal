import logging

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.config import get_database_url, is_postgres
from app.models import Base
from app.unit_of_work import SqlAlchemyUnitOfWork

logger = logging.getLogger(__name__)


def _create_engine(url: str | None = None) -> Engine:
    """Create a SQLAlchemy engine configured for the given database URL.

    - SQLite: uses check_same_thread=False for FastAPI compatibility.
    - PostgreSQL: uses NullPool so Supabase's Supavisor handles pooling.
    """
    if url is None:
        url = get_database_url()

    if is_postgres(url):
        return create_engine(url, poolclass=NullPool)

    # SQLite
    return create_engine(url, connect_args={"check_same_thread": False})


engine = _create_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _add_missing_columns() -> None:
    """Add any columns defined in the models but missing from the database.

    This handles the common case where new nullable columns are added to a
    model after the database has already been created. Only nullable columns
    with no server default are handled (safe for ALTER TABLE ADD COLUMN in
    SQLite).

    This helper is SQLite-only. PostgreSQL schemas should be managed
    exclusively through Alembic migrations.
    """
    if is_postgres():
        return

    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue  # table will be created by create_all
            existing = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue

                # Keep this helper limited to SQLite-safe additions only.
                # Non-nullable columns or columns with server defaults should
                # be introduced through a proper migration instead.
                if not column.nullable or column.server_default is not None:
                    continue

                col_type = column.type.compile(dialect=engine.dialect)
                conn.execute(
                    text(
                        f"ALTER TABLE {table.name} ADD COLUMN {column.name} {col_type}"
                    )
                )
                logger.info("Added column %s to %s table", column.name, table.name)


def init_db() -> None:
    """Initialize the database schema.

    For SQLite: creates tables via metadata and patches missing columns.
    For PostgreSQL: schema is managed exclusively by Alembic. Logs a
    reminder if Alembic has not been run.
    """
    if is_postgres():
        logger.info(
            "PostgreSQL detected — schema managed by Alembic. "
            "Run 'alembic upgrade head' to apply migrations."
        )
        return

    Base.metadata.create_all(bind=engine)
    _add_missing_columns()


def get_db():
    """Yield a database session, ensuring it is closed after use.

    .. deprecated::
        Prefer :func:`get_uow` for new code.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_uow():
    """Yield a :class:`SqlAlchemyUnitOfWork` wrapping a fresh session.

    This is the preferred FastAPI dependency for route handlers.
    """
    session = SessionLocal()
    uow = SqlAlchemyUnitOfWork(session)
    try:
        yield uow
    finally:
        session.close()
