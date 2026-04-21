import logging

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.models import Base

logger = logging.getLogger(__name__)

DATABASE_URL = "sqlite:///./stocksentinal.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _add_missing_columns() -> None:
    """Add any columns defined in the models but missing from the database.

    This handles the common case where new nullable columns are added to a
    model after the database has already been created. Only nullable columns
    with no server default are handled (safe for ALTER TABLE ADD COLUMN in
    SQLite).
    """
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
    """Create all database tables and add any missing columns."""
    Base.metadata.create_all(bind=engine)
    _add_missing_columns()


def get_db():
    """Yield a database session, ensuring it is closed after use."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
